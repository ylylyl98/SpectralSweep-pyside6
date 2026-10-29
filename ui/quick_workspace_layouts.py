"""Quick-only assembly of existing workflow controls into useful workspaces.

The page still owns its original controls, validation and session state. These
changes run between table and scalar migration; the Widgets path is untouched.
"""
import math
from types import MethodType

from PySide6.QtCore import QEvent, QObject, QTimer, Qt
from PySide6.QtWidgets import (QApplication, QBoxLayout, QFormLayout, QGridLayout,
                               QGroupBox, QLabel, QSizePolicy, QVBoxLayout, QWidget)

from ui.workflow_sections import DisclosureSection


def _valid_splitter_sizes(value):
    if (isinstance(value, (list, tuple)) and len(value) == 2
            and all(isinstance(size, (int, float)) and not isinstance(size, bool)
                    and math.isfinite(size) and size > 0 for size in value)):
        return tuple(value)
    return None


def remember_workspace_layout(panel, state):
    """Record successful startup restoration before lazy Quick migration.

    MainWindow restores source pages before constructing QuickWorkbench. This
    explicit evidence distinguishes saved UI preferences from source defaults;
    current splitter geometry alone cannot establish that distinction.
    """
    if not isinstance(state, dict):
        return
    sections = state.get("sections")
    if isinstance(sections, dict) and "optical" in sections:
        panel._quick_restored_optical_expanded = bool(sections["optical"])
    sizes = _valid_splitter_sizes(state.get("splitter_sizes"))
    ratio = state.get("splitter_ratio")
    if not (isinstance(ratio, (int, float)) and not isinstance(ratio, bool)
            and math.isfinite(ratio) and 0 < ratio < 1):
        ratio = None
    if sizes is not None or ratio is not None:
        panel._quick_restored_workspace_layout = {
            "sizes": sizes, "ratio": ratio,
            "orientation": state.get("splitter_orientation"),
        }


class _WorkspacePaneWidth(QObject):
    """Choose a readable initial pane width once the real viewport exists.

    QSplitter's construction-time sizes are weights, so applying [600, 340]
    before a desktop window is laid out can produce a 1000px settings pane.
    Normally seed only the initial geometry. A responsive default can follow
    the viewport until dragging or explicit session restoration takes over.
    """

    def __init__(self, panel, splitter, preferred, *, index=0, state_key="splitter_sizes", responsive=False):
        super().__init__(splitter)
        self.splitter = splitter
        self.preferred = preferred
        self.index = index
        self.responsive = responsive
        self.initialized = False
        self.last_width = None
        self.on_width_applied = None
        restored = getattr(panel, "_quick_restored_workspace_layout", {})
        self.restored_sizes = restored.get("sizes") if state_key else None
        self.restored_orientation = restored.get("orientation") if state_key else None
        splitter.setStretchFactor(index, 0)
        splitter.setStretchFactor(1 - index, 1)
        splitter.installEventFilter(self)
        splitter.splitterMoved.connect(self._moved)
        restore = getattr(panel, "restore_session_state", None)
        if callable(restore) and state_key:
            def restore_state(_panel, state):
                sizes = _valid_splitter_sizes(state.get(state_key)) if isinstance(state, dict) else None
                if sizes is not None:
                    # A caller restoring its own layout takes precedence over
                    # the presentation default, even before the first show.
                    self.initialized = splitter.isVisible()
                    self.responsive = False
                    self.restored_sizes = sizes
                    self.restored_orientation = state.get("splitter_orientation")
                result = restore(state)
                if self.initialized and splitter.orientation() == Qt.Orientation.Horizontal:
                    self.last_width = splitter.sizes()[index]
                return result
            panel.restore_session_state = MethodType(restore_state, panel)
        QTimer.singleShot(0, self, self.initialize)

    def eventFilter(self, watched, event):
        if event.type() in (QEvent.Type.Show, QEvent.Type.Resize):
            callback = self.recover_width if self.initialized else self.initialize
            QTimer.singleShot(0, self, callback)
        return False

    def initialize(self):
        splitter = self.splitter
        if self.initialized or not splitter.isVisible() or splitter.orientation() != Qt.Orientation.Horizontal:
            return
        available = splitter.width() - splitter.handleWidth()
        if available <= 0:
            return
        preferred = self.preferred(available) if callable(self.preferred) else self.preferred
        if self.restored_sizes and self.restored_orientation in (None, "Horizontal"):
            preferred = available * self.restored_sizes[self.index] / sum(self.restored_sizes)
        other = splitter.widget(1 - self.index)
        width = min(int(preferred), max(0, available - other.minimumWidth()))
        sizes = [available - width, available - width]
        sizes[self.index] = width
        if splitter.sizes() != sizes:
            splitter.setSizes(sizes)
        self.initialized = True
        self.last_width = splitter.sizes()[self.index]
        if self.on_width_applied is not None:
            self.on_width_applied()

    def recover_width(self):
        """Recover a drag preference after a temporary minimum-width clamp."""
        splitter = self.splitter
        if self.last_width is None or splitter.orientation() != Qt.Orientation.Horizontal:
            return
        if self.responsive and not self.restored_sizes:
            self.initialized = False
            self.initialize()
            return
        available = splitter.width() - splitter.handleWidth()
        other = splitter.widget(1 - self.index)
        other_minimum = max(other.minimumWidth(), other.minimumSizeHint().width())
        # Qt handles constrained windows. Only restore once there is room for
        # the original width again; ordinary resize events keep native sizing.
        if available >= self.last_width + other_minimum and splitter.sizes()[self.index] != self.last_width:
            sizes = [available - self.last_width, available - self.last_width]
            sizes[self.index] = self.last_width
            splitter.setSizes(sizes)
            if self.on_width_applied is not None:
                self.on_width_applied()

    def _moved(self, *_args):
        if self.splitter.orientation() == Qt.Orientation.Horizontal:
            self.responsive = False
            self.initialized = True
            self.last_width = self.splitter.sizes()[self.index]


def _readable_pane(panel, splitter, preferred, **kwargs):
    policy = _WorkspacePaneWidth(panel, splitter, preferred, **kwargs)
    panel._quick_pane_width = policy
    return policy


def _compact_sections(content):
    """Keep semantic sections at their content height, with spare room below."""
    if content.layout() is not None:
        content.layout().setAlignment(Qt.AlignmentFlag.AlignTop)
    for widget in content.findChildren(QWidget, options=Qt.FindChildOption.FindDirectChildrenOnly):
        if isinstance(widget, (QGroupBox, DisclosureSection)):
            widget.setSizePolicy(widget.sizePolicy().horizontalPolicy(), QSizePolicy.Policy.Maximum)


def _workspace_surface(widget):
    """Two large task surfaces provide hierarchy without nested cards."""
    widget.setProperty("quickWorkspaceSurface", True)
    widget.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
    widget.style().unpolish(widget)
    widget.style().polish(widget)
    # A splitter/tab owns a private Qt layout; only inset application layouts.
    if type(widget) is QWidget and widget.layout() is not None:
        widget.layout().setContentsMargins(12, 12, 12, 12)
        widget.layout().setSpacing(8)


class _WideOpticalFooter(QObject):
    """Keep optical recipes with parameters and summarize short viewports.

    Only the untouched default follows the available height. Explicit section
    choices, including restored sessions, always take precedence on resize.
    """

    def __init__(self, panel):
        super().__init__(panel)
        self.panel = panel
        self._changing_default = False
        # Recipes contain wavelength, exposure, frames and detector choices.
        # Keep the same controls in the settings column at every window width;
        # the independent right-hand surface owns monitoring and run actions.
        scroll = panel._optical_scroll
        scroll.parentWidget().layout().removeWidget(scroll)
        panel._left_lay.insertWidget(0, scroll)
        section = panel._optical_group
        self.explicit_expansion = getattr(panel, "_quick_restored_optical_expanded", None)
        if self.explicit_expansion is None and not section.is_expanded():
            self.explicit_expansion = False
        section._header.setProperty("quickDisclosure", True)
        section._header.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Fixed)
        section._header.setAccessibleName("Optical Sequence")
        section.expanded_changed.connect(self._expansion_changed)
        panel._optical_widget.changed.connect(self.update_summary)
        restore = panel.restore_session_state
        def restore_state(_panel, state):
            sections = state.get("sections") if isinstance(state, dict) else None
            if isinstance(sections, dict) and "optical" in sections:
                # Restoring the already-current value emits no toggled signal.
                self.explicit_expansion = bool(sections["optical"])
            return restore(state)
        panel.restore_session_state = MethodType(restore_state, panel)
        self.timer = QTimer(self)
        self.timer.setSingleShot(True)
        self.timer.timeout.connect(self.fit)
        panel.installEventFilter(self)
        panel._optical_scroll.viewport().installEventFilter(self)
        self.timer.start(0)

    def _expansion_changed(self, expanded):
        if not self._changing_default:
            self.explicit_expansion = expanded
        self.update_summary()

    def update_summary(self):
        section = self.panel._optical_group
        conditions = self.panel._optical_widget.conditions()
        active = [condition for condition in conditions if condition.enabled]
        summary = f"{len(active)}/{len(conditions)} active"
        if conditions:
            primary = (active or conditions)[0]
            # Names remain editable in the original table; keep this glanceable
            # summary bounded even when an imported recipe has a long name.
            name = primary.name if len(primary.name) <= 28 else primary.name[:27] + "…"
            frames = "frame" if primary.frames == 1 else "frames"
            summary += (f"\n{name} · {primary.center_nm:g} nm · "
                        f"{primary.exposure_ms:g} ms · {primary.frames} {frames}")
        title = "Optical Sequence"
        section._header.setText(title if section.is_expanded() else title + " · " + summary)
        action = "Collapse" if section.is_expanded() else "Expand to edit"
        section._header.setToolTip(f"{action} Optical Sequence. {summary}")
        # QAbstractButton has no textChanged signal. Its adapter observes Qt
        # property changes, so publish this readback without waiting for its
        # periodic fallback or forcing a refresh of unrelated scalar fields.
        readback = section._header.text() + "\n" + summary
        if section._header.property("quickOpticalSummary") != readback:
            section._header.setProperty("quickOpticalSummary", readback)

    def eventFilter(self, watched, event):
        if event.type() in (QEvent.Type.Resize, QEvent.Type.Show):
            self.timer.start(0)
        return False

    def fit(self):
        panel = self.panel
        if not panel.isVisible():
            return
        if self.explicit_expansion is None:
            self._changing_default = True
            try:
                panel._optical_group.set_expanded(panel.height() >= 800)
            finally:
                self._changing_default = False
        self.update_summary()
        # The source remains responsible for row-count and collapse heights.
        # Quick recipe records wrap inside the parameter column as it narrows.
        panel._refresh_settings_scroll_range()


def _layout_slot(layout, widget):
    for index in range(layout.count()):
        item = layout.itemAt(index)
        if item.widget() is widget:
            return layout, index
        if item.layout() is not None:
            found = _layout_slot(item.layout(), widget)
            if found:
                return found
    return None


def _fold(widget, title, *, expanded=False):
    owner = widget.parentWidget()
    found = _layout_slot(owner.layout(), widget) if owner and owner.layout() else None
    if not found:
        return None
    layout, index = found
    position = layout.getItemPosition(index) if isinstance(layout, (QGridLayout, QFormLayout)) else None
    alignment = layout.itemAt(index).alignment()
    stretch = layout.stretch(index) if isinstance(layout, QBoxLayout) else 0
    layout.takeAt(index)
    section = DisclosureSection(title, widget, expanded=expanded, parent=owner)
    section.setObjectName("QuickOptional_" + title.split(" · ")[0].replace(" ", ""))
    section.toggle.setToolTip(title + ". Expand to review or edit these settings.")
    if isinstance(widget, QGroupBox) and not widget.isCheckable():
        widget.setTitle("")
    if isinstance(layout, QGridLayout):
        layout.addWidget(section, *position, alignment)
    elif isinstance(layout, QFormLayout):
        layout.setWidget(*position, section)
    else:
        layout.insertWidget(index, section, stretch, alignment)
    return section


def _group(panel, title):
    return next((group for group in panel.findChildren(QGroupBox) if group.title() == title), None)


def _adopt_item(item, owner):
    if item.widget() is not None:
        widget = item.widget()
        hidden = widget.isHidden()
        widget.setParent(owner)
        widget.setVisible(not hidden)
    elif item.layout() is not None:
        for index in range(item.layout().count()):
            _adopt_item(item.layout().itemAt(index), owner)


def _move_form_rows(form, targets, title):
    targets = set(targets)
    rows = []
    def contains(item):
        if item is None:
            return False
        if item.widget() is not None:
            widget = item.widget()
            return widget in targets or any(widget.isAncestorOf(target) for target in targets)
        return item.layout() is not None and any(contains(item.layout().itemAt(i)) for i in range(item.layout().count()))
    for row in range(form.rowCount()):
        if contains(form.itemAt(row, QFormLayout.ItemRole.FieldRole)) or contains(form.itemAt(row, QFormLayout.ItemRole.SpanningRole)):
            rows.append(row)
    if not rows:
        return None
    content = QWidget()
    details = QFormLayout(content)
    details.setContentsMargins(0, 0, 0, 0)
    moved = [form.takeRow(row) for row in reversed(rows)]
    for row, taken in enumerate(reversed(moved)):
        for item, role in ((taken.labelItem, QFormLayout.ItemRole.LabelRole),
                           (taken.fieldItem, QFormLayout.ItemRole.FieldRole)):
            if item is not None:
                _adopt_item(item, content)
                details.setItem(row, role, item)
    section = DisclosureSection(title, content)
    form.addRow(section)
    return section


def _fold_groups(panel, specifications):
    result = {}
    for source_title, disclosure_title in specifications:
        group = _group(panel, source_title)
        if group is not None:
            result[source_title] = _fold(group, disclosure_title)
    return result


def _mcd_layout(panel, page_id):
    # Routine identity/range values do not need path-sized text fields. These
    # are presentation hints consumed by the scalar adapter, never input limits.
    for name, width in {"_sample_id": 160, "_point": 140, "_temperature": 140,
                        "_start_t": 120, "_stop_t": 120, "_angle_a": 120,
                        "_angle_b": 120, "_rotator": 120, "_sweep_mode": 120,
                        "_start_settle": 120, "_rotation_settle": 120,
                        "_center": 120, "_exposure": 120, "_frames": 100,
                        "start_field": 120, "stop_field": 120, "angles": 140,
                        "rotator": 120, "lf_center": 120, "lf_exposure": 120,
                        "lf_frames": 100}.items():
        widget = getattr(panel, name, None)
        if isinstance(widget, QWidget):
            widget.setProperty("quickPreferredWidth", width)
    # Paired sweep/rotation sections have room for two compact fields per row
    # when their labels sit above the controls instead of consuming a column.
    for name in ("_start_t", "_stop_t", "_start_settle", "_sweep_mode",
                 "_rotator", "_rotation_settle", "_angle_a", "_angle_b",
                 "start_field", "stop_field", "angles", "rotator",
                 "_center", "_exposure", "_frames",
                 "lf_center", "lf_exposure", "lf_frames"):
        widget = getattr(panel, name, None)
        if isinstance(widget, QWidget):
            widget.setProperty("quickLabelAbove", True)
    result = _fold_groups(panel, [
        ("APS100 — attoDRY1000", "Magnet connection and diagnostics"),
        ("attoDRY2100 connection and telemetry", "Magnet connection and telemetry"),
        ("New gate rows", "Add gate conditions · optional editor"),
        ("Gate run preview", "Gate plan details · selected condition"),
        ("Filename", "Output filename · preview and tokens"),
    ])
    if page_id == "mcd2100":
        panel._workflow_layout.setSpacing(4)
        # The notice already describes the round trip under "Field Sweep";
        # its extra "Sweep" caption otherwise occupies an entire narrow row.
        sweep_label = panel._sweep_group.layout().labelForField(panel.round_trip_notice)
        if sweep_label is not None:
            sweep_label.hide()
        temperature = _group(panel, "Temperature")
        if temperature is not None:
            result["Temperature"] = _fold(temperature, "Temperature control · optional",
                expanded=panel.temperature_control_enabled.isChecked())
    scroll = getattr(panel, "_settings_scroll", getattr(panel, "_scroll", None))
    if scroll is not None:
        scroll.setMinimumWidth(440)
    rows = [getattr(panel, name, None) for name in ("_row_a", "_field_rotation_row", "_temperature_lightfield_row")]
    rows = [row for row in rows if row is not None]
    if rows:
        for row in rows:
            # Keep these semantic columns available after scalar migration,
            # including when a narrow or collapsed initial state is stacked.
            row.setProperty("quickSectionColumns", True)
        panel._quick_legacy_responsive_layout = panel._apply_responsive_layout
        def fit(self):
            side_by_side = scroll.viewport().width() >= 500
            for row in rows:
                columns = side_by_side
                if row is getattr(self, "_temperature_lightfield_row", None):
                    section = result.get("Temperature")
                    # A collapsed temperature header should not reserve half
                    # the row while the optical fields stack in the other half.
                    columns = columns and bool(section and section.toggle.isChecked())
                row.setDirection(QBoxLayout.Direction.LeftToRight if columns else QBoxLayout.Direction.TopToBottom)
                row.setStretch(0, 1 if columns else 0)
                row.setStretch(1, 1 if columns else 0)
        panel._apply_responsive_layout = MethodType(fit, panel)
        if result.get("Temperature") is not None:
            result["Temperature"].toggle.toggled.connect(lambda _: panel._apply_responsive_layout())
        panel._apply_responsive_layout()
    if scroll is not None:
        _compact_sections(scroll.widget())
        _workspace_surface(scroll.widget())
        scroll.widget().layout().setSpacing(6)
    _workspace_surface(panel._splitter.widget(1))
    # The 2100 condition table includes its input-mode selector and an index;
    # a little more width keeps every gate value on one inline band.
    _readable_pane(panel, panel._splitter, 660 if page_id == "mcd2100" else 620)
    return result


def _motion_layout(panel):
    result = {}
    for group in (panel._conditions_grp, panel._cal_grp):
        # Both original handlers hide their entire bodies when unchecked.
        # The Quick enable header remains visible outside that empty body.
        group.setProperty("quickCollapseWhenUnchecked", True)
    # These choices and bounded voltage values do not need path-sized editors.
    # Retain their ranges/precision while keeping the three gates on one row.
    for name, width in {"_input_mode": 140, "_vbg_spin": 130,
                        "_vtg_spin": 130, "_vbias_spin": 130}.items():
        widget = getattr(panel, name, None)
        if isinstance(widget, QWidget):
            widget.setProperty("quickPreferredWidth", width)
    metadata = _group(panel, "File / Metadata")
    if metadata is not None:
        grid = metadata.layout()
        details = QWidget()
        detail_grid = QGridLayout(details)
        detail_grid.setContentsMargins(0, 0, 0, 0)
        entries = []
        for index in reversed(range(grid.count())):
            position = grid.getItemPosition(index)
            if position[0] > 0:
                entries.append((grid.takeAt(index), position))
        for item, (row, column, row_span, column_span) in reversed(entries):
            _adopt_item(item, details)
            detail_grid.addItem(item, row - 1, column, row_span, column_span)
        section = DisclosureSection("Output details · laser and filename", details)
        grid.addWidget(section, 1, 0, 1, grid.columnCount())
        result["Output details"] = section
    form = panel._motion_form
    for row in range(form.rowCount()):
        item = form.itemAt(row, QFormLayout.ItemRole.FieldRole)
        if item and isinstance(item.layout(), QGridLayout):
            grid = item.layout()
            for column in range(3):
                heading = grid.itemAtPosition(0, column)
                if heading and isinstance(heading.widget(), QLabel) and heading.widget().text() in {"Axis", "Mode", "Values"}:
                    heading.widget().hide()
    preview = _move_form_rows(form, [panel._pos_preview_lbl, panel._power_preview_lbl],
                              "Position and power preview")
    if preview is not None:
        def update_preview_title(*args):
            count = panel._pos_count_lbl.text()
            preview.toggle.setText("Position preview · " + (count or "check motion input"))
        for edit in panel._axis_value_edits.values():
            edit.textChanged.connect(update_preview_title)
        update_preview_title()
        # The disclosure title already carries this live count. Keep the
        # source label for the controller, without a duplicate standalone row.
        form.setRowVisible(panel._pos_count_lbl, False)
        result["Position preview"] = preview
    _compact_sections(panel._splitter.widget(0).widget())
    _workspace_surface(panel._splitter.widget(0).widget())
    _workspace_surface(panel._splitter.widget(1))
    _readable_pane(panel, panel._splitter, 610)
    return result


def _bfp_layout(panel):
    result = {}
    naming = _group(panel, "Save Name")
    if naming is not None:
        result["Naming"] = _move_form_rows(naming.layout(),
            [panel._material_combo, panel._power_edit, panel._note_edit,
             panel._suffix_edit, panel._run_idx_spin, panel._stem_lbl],
            "Naming details · material and file tokens")
    acquisition = _group(panel, "Acquisition Settings")
    if acquisition is not None:
        result["Acquisition options"] = _move_form_rows(acquisition.layout(),
            [panel._auto_save_csv_chk, panel._auto_apply_chk, panel._warmup_chk],
            "Acquisition options · save and warm-up")
    bg = panel._bg_panel
    source_layout = bg.parentWidget().layout()
    source_layout.removeWidget(panel._apply_bg_btn)
    bg.layout().addRow(panel._apply_bg_btn)
    result["Background"] = _fold(bg, "Background correction · optional",
                                expanded=bg._enable_chk.isChecked())
    panel._settings_scroll.setMaximumWidth(560)
    _compact_sections(panel._settings_scroll.widget())
    _workspace_surface(panel._settings_scroll.widget())
    _workspace_surface(panel._splitter.widget(1))
    _readable_pane(panel, panel._splitter, 470)
    return result


def _dual_gate_layout(panel):
    layout = panel._results_content.layout()
    safety_content = QWidget()
    safety_layout = QVBoxLayout(safety_content)
    safety_layout.setContentsMargins(0, 0, 0, 0)
    safety_layout.setSpacing(6)
    for bar in (panel._safety_bar, panel._voltage_timing_bar):
        layout.removeWidget(bar)
        bar.setMaximumHeight(16777215)
        safety_layout.addWidget(bar)
    safety = DisclosureSection("Safety and settling", safety_content, expanded=False)
    safety.setObjectName("QuickDualGateSafetyDisclosure")
    summary = QLabel()
    summary.setObjectName("QuickDualGateSafetySummary")
    summary.setWordWrap(True)
    safety.layout().insertWidget(1, summary)

    def update_summary(*args):
        text = (f"Max jump {panel._safe_jump_spin.value():g} V · "
                f"settle {panel._initial_voltage_settle_spin.value():g} / "
                f"{panel._voltage_settle_spin.value():g} s")
        summary.setText(text)
        safety.toggle.setToolTip(text + ". Expand to edit safety and settling limits.")

    for spin in (panel._safe_jump_spin, panel._initial_voltage_settle_spin,
                 panel._voltage_settle_spin):
        spin.valueChanged.connect(update_summary)
    update_summary()

    preview_splitter = panel._preview_splitter
    execution = DisclosureSection("Execution order", panel._execution_order_group, expanded=False)
    execution.setObjectName("QuickDualGateExecutionDisclosure")
    # Reparenting the original group removes it from the splitter. Reinsert a
    # disclosure in the same slot so saved two-part splitter state remains valid.
    preview_splitter.insertWidget(0, execution)
    execution.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
    panel._execution_order_group.setTitle("")

    sequence_panel = panel._tree.parentWidget()

    def fit_execution(*args):
        # The compact Quick view publishes a content height: short plans need
        # one card, long plans retain a bounded scrolling viewport. Do not let
        # this splitter reserve the old 380px minimum or absorb spare space.
        expanded = execution.toggle.isChecked()
        execution.setMaximumHeight(16777215 if expanded else 36)
        execution.setMinimumHeight(0)
        execution.updateGeometry()
        sequence_layout = sequence_panel.layout()
        sequence_layout.invalidate()
        width = max(1, sequence_panel.width())
        sequence_height = (sequence_layout.totalHeightForWidth(width)
                           if sequence_layout.hasHeightForWidth()
                           else sequence_layout.sizeHint().height())
        sequence_panel.setFixedHeight(sequence_height)
        execution_height = (execution.heightForWidth(width) if execution.hasHeightForWidth()
                            else execution.sizeHint().height()) if expanded else 36
        preview_splitter.setFixedHeight(execution_height + sequence_height + preview_splitter.handleWidth())
        preview_splitter.setSizes([execution_height, sequence_height])

    execution.toggle.toggled.connect(fit_execution)
    if hasattr(panel._tree, "preferredHeightChanged"):
        panel._tree.preferredHeightChanged.connect(fit_execution)
    panel._tree.setMinimumHeight(0)
    panel._tree.setMaximumHeight(16777215)
    fit_execution(False)
    preview_splitter.setStretchFactor(0, 0)
    preview_splitter.setStretchFactor(1, 1)
    layout.removeWidget(preview_splitter)
    layout.insertWidget(0, preview_splitter)
    layout.setAlignment(Qt.AlignmentFlag.AlignTop)
    layout.insertWidget(1, safety)
    filename_group = panel._filename_preview_lbl.parentWidget()
    layout.removeWidget(filename_group)
    filename = DisclosureSection("Filename preview", filename_group, expanded=False)
    filename.setObjectName("QuickDualGateFilenameDisclosure")
    filename_group.setTitle("")
    layout.insertWidget(2, filename)

    # QuickWorkbench mounts compact Apply/Run/Stop below the right scroll area.
    # Keeping the former tall footer fixed would clip a 768px laptop viewport.
    # Keep live progress beside those fixed actions; optional tools can scroll
    # inside Run details without hiding acquisition feedback.
    results_layout = panel._results_pane.layout()
    footer = results_layout.itemAt(1).widget()
    results_layout.removeWidget(footer)
    footer.layout().removeWidget(panel._readiness_lbl)
    layout.insertWidget(1, panel._readiness_lbl)
    run_status = QWidget(panel._results_pane)
    run_status.setObjectName("QuickDualGateRunStatus")
    run_status.setProperty("quickSectionBoundary", True)
    status_layout = QVBoxLayout(run_status)
    status_layout.setContentsMargins(8, 4, 8, 4)
    status_layout.setSpacing(2)
    for name in ("_progress", "_status_lbl", "_run_time_lbl"):
        source = getattr(panel, name, None)
        if source is not None:
            footer.layout().removeWidget(source)
            status_layout.addWidget(source)
    run_status.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Maximum)
    results_layout.addWidget(run_status)
    run_details = DisclosureSection("Run details", footer, expanded=False)
    run_details.setObjectName("QuickDualGateRunDetailsDisclosure")
    layout.addWidget(run_details)
    panel._quick_safety_disclosure = safety
    panel._quick_execution_disclosure = execution
    panel._quick_filename_disclosure = filename
    panel._quick_run_details_disclosure = run_details
    panel._quick_run_status = run_status
    for section in (safety, filename, run_details):
        section.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Maximum)
    panel._readiness_lbl.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Maximum)
    _compact_sections(panel._workflow_scroll.widget())
    # Give the main editing table all remaining workflow height. Keep the
    # calculator immediately below its toolbar, at the bottom when collapsed.
    # An expanded calculator remains in the same vertical scroller.
    panel._workflow_layout.setAlignment(Qt.AlignmentFlag(0))
    panel._batch_group.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
    panel._sweep_calc.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Maximum)
    from ui.quick_sweep_calculator import configure_sweep_calculator
    configure_sweep_calculator(panel)
    _workspace_surface(panel._workflow_scroll.widget())
    _workspace_surface(panel._results_content)
    # One right-hand card encloses both the scrollable preview and its fixed
    # actions. Keep the existing content inset, without a second inner border.
    _workspace_surface(panel._results_pane)
    panel._results_pane.layout().setContentsMargins(1, 1, 1, 1)
    panel._results_pane.layout().setSpacing(0)
    panel._results_content.setProperty("quickWorkspaceSurface", False)
    panel._results_content.style().unpolish(panel._results_content)
    panel._results_content.style().polish(panel._results_content)
    # The sequence is a compact inspector. Give extra desktop width to the
    # editable table, rather than stretching the preview into a second canvas.
    _readable_pane(panel, panel._splitter,
                   lambda available: min(420, max(350, available * .36)),
                   index=1, responsive=True)
    panel._quick_legacy_fit_workflow_layout = panel._fit_workflow_layout

    def fit_quick_workflow(self):
        horizontal = self.width() >= 820
        target = Qt.Orientation.Horizontal if horizontal else Qt.Orientation.Vertical
        self._workflow_scroll.setMinimumWidth(420 if horizontal else 0)
        self._results_scroll.setMinimumWidth(350 if horizontal else 0)
        self._workflow_scroll.setMinimumHeight(0 if horizontal else 240)
        self._results_pane.setMinimumHeight(0 if horizontal else 340)
        if self._splitter.orientation() != target:
            self._splitter.setOrientation(target)
            extent = self._splitter.width() if horizontal else self._splitter.height()
            self._splitter.setSizes([int(extent * .55), int(extent * .45)])
        fit_execution()

    # The existing resize/session code already calls this presentation hook.
    # Replace it on this Quick page instance, retaining the Widgets class method.
    panel._fit_workflow_layout = MethodType(fit_quick_workflow, panel)
    panel._fit_workflow_layout()
    QTimer.singleShot(0, panel, panel._fit_workflow_layout)
    return {"safety": safety, "execution": execution, "filename": filename,
            "run_details": run_details, "run_status": run_status, "sequence": panel._tree}


def _mega_sweep_layout(panel):
    hint = panel._raw_link_label
    panel._settings_grid.removeWidget(hint)
    # The ratio explanation belongs to the coordinate controls. A separate
    # scalar run spanning the settings grid could place it over the first title.
    coord_layout = panel._coord_widget.layout()
    coord_layout.addWidget(hint)
    margins = coord_layout.contentsMargins()
    coord_layout.setContentsMargins(margins.left(), 6,
                                   margins.right(), margins.bottom())
    coord_layout.setSpacing(8)
    hint.setMinimumHeight(hint.fontMetrics().height() * 2)
    panel._preview.setMinimumHeight(220)
    from ui.quick_tab_layout import install_adaptive_preview_tabs
    install_adaptive_preview_tabs(panel._preview._tabs)
    policy = _readable_pane(panel, panel._splitter, 620, state_key=None)
    policy.on_width_applied = panel._on_splitter_moved
    restored = getattr(panel, "_quick_restored_workspace_layout", {})
    if restored.get("ratio") is not None:
        ratio = min(.55, max(.25, restored["ratio"]))
        policy.restored_sizes = (ratio, 1 - ratio)
    elif restored.get("sizes") is not None:
        policy.restored_sizes = restored["sizes"]
    # The Widgets page preserves a percentage on every resize. Quick uses a
    # stable parameter width and lets the scientific preview absorb growth.
    panel._quick_legacy_horizontal_splitter_sizes = panel._apply_horizontal_splitter_sizes
    panel._quick_returning_horizontal = False
    original_fit = panel._apply_responsive_layout
    def fit(self, *, force=False):
        if self._splitter.orientation() == Qt.Orientation.Vertical:
            self._quick_returning_horizontal = True
        original_fit(force=force)
    def horizontal_sizes(self):
        if self._quick_returning_horizontal and policy.initialized and policy.last_width is not None:
            available = self._splitter.width() - self._splitter.handleWidth()
            self._splitter.setSizes([policy.last_width, max(420, available - policy.last_width)])
            self._quick_returning_horizontal = False
        else:
            policy.initialize()
        if policy.initialized:
            self._on_splitter_moved()
    panel._apply_responsive_layout = MethodType(fit, panel)
    panel._apply_horizontal_splitter_sizes = MethodType(horizontal_sizes, panel)
    restore = panel.restore_session_state
    def restore_state(self, state):
        result = restore(state)
        if isinstance(state, dict) and ("splitter_ratio" in state or "splitter_sizes" in state):
            policy.initialized = self._splitter.isVisible()
            restored_ratio = self._horizontal_splitter_ratio
            policy.restored_sizes = (restored_ratio, 1 - restored_ratio)
            def restore_width():
                if self._splitter.isVisible() and self._splitter.orientation() == Qt.Orientation.Horizontal:
                    # The legacy force callback records the pre-restore
                    # geometry before applying its ratio. Retain the value
                    # just restored by the source session parser.
                    self._horizontal_splitter_ratio = restored_ratio
                    self._quick_legacy_horizontal_splitter_sizes()
                    policy.last_width = self._splitter.sizes()[0]
            QTimer.singleShot(0, self, restore_width)
        return result
    panel.restore_session_state = MethodType(restore_state, panel)
    _compact_sections(panel._settings_scroll.widget())
    _workspace_surface(panel._settings_scroll.widget())
    _workspace_surface(panel._right_panel)
    panel._quick_optical_footer = _WideOpticalFooter(panel)
    return {"coordinate_hint": hint}


def configure_workspace_layout(panel, page_id):
    """Assemble Quick-only workflow layout; call before scalar form migration."""
    app = QApplication.instance()
    if app is None or not app.property("spectralQuick"):
        return {}
    existing = getattr(panel, "_quick_workspace_layout", None)
    if existing is not None:
        return existing
    if page_id == "dual_gate" and hasattr(panel, "_results_content"):
        result = _dual_gate_layout(panel)
    elif page_id == "mega_sweep" and hasattr(panel, "_raw_link_label"):
        result = _mega_sweep_layout(panel)
    elif page_id in {"mcd", "mcd2100"} and hasattr(panel, "_condition_table"):
        result = _mcd_layout(panel, page_id)
    elif page_id == "power_sweep" and hasattr(panel, "_motion_grp"):
        result = _motion_layout(panel)
    elif page_id == "bfp" and hasattr(panel, "_bg_panel"):
        result = _bfp_layout(panel)
    elif page_id == "spectrum" and hasattr(panel, "_content_splitter"):
        _workspace_surface(panel._tabs)
        _workspace_surface(panel._side_panel)
        _readable_pane(panel, panel._content_splitter, 340, index=1, state_key=None)
        result = {}
    elif page_id == "settings":
        from ui.quick_settings_layout import configure_quick_settings
        result = configure_quick_settings(panel)
    elif page_id == "calibration":
        from ui.quick_calibration_layout import configure_quick_calibration
        result = configure_quick_calibration(panel)
    else:
        result = {}
    if page_id in {"dual_gate", "mega_sweep"}:
        from ui.quick_gate_sections import configure_gate_sections
        configure_gate_sections(panel, page_id)
    if page_id in {"spectrum", "power_sweep", "mcd", "mcd2100"}:
        from ui.quick_acquisition_sections import configure_acquisition_sections
        configure_acquisition_sections(panel, page_id)
    if page_id == "instruments":
        from ui.quick_instrument_layouts import configure_instrument_layout
        result = configure_instrument_layout(panel)
    panel._quick_workspace_layout = result
    return result
