"""Quick-only assembly of existing workflow controls into useful workspaces.

The page still owns its original controls, validation and session state. These
changes run between table and scalar migration; the Widgets path is untouched.
"""
import math
from types import MethodType

from PySide6.QtCore import QEvent, QObject, QTimer, Qt
from PySide6.QtWidgets import (QApplication, QBoxLayout, QFormLayout, QGridLayout,
                               QFrame, QGroupBox, QLabel, QScrollArea, QSizePolicy, QVBoxLayout, QWidget)

from ui.workflow_sections import DisclosureSection
from ui.workspace_pane import WorkspacePaneWidth as _WorkspacePaneWidth, valid_splitter_sizes as _valid_splitter_sizes


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


class _RunDetailsHeight(QObject):
    """Bound upward-opening details while retaining room for live run controls."""

    def __init__(self, pane, preview, section, scroll, content):
        super().__init__(section)
        self.pane, self.preview, self.section = pane, preview, section
        self.scroll, self.content = scroll, content
        self.timer = QTimer(self)
        self.timer.setSingleShot(True)
        self.timer.timeout.connect(self.fit)
        for widget in (pane, content, scroll.viewport(), section):
            widget.installEventFilter(self)
        section.toggle.toggled.connect(lambda *_: self.timer.start(0))
        self.timer.start(0)

    def eventFilter(self, watched, event):
        if event.type() in (QEvent.Type.Resize, QEvent.Type.LayoutRequest, QEvent.Type.Show):
            self.timer.start(0)
        return False

    def fit(self):
        if not self.section.toggle.isChecked():
            return
        layout = self.pane.layout()
        fixed = sum(item.sizeHint().height() for item in
                    (layout.itemAt(i) for i in range(layout.count()))
                    if item.widget() not in (self.preview, self.section) and not item.isEmpty())
        header = self.section.toggle.sizeHint().height() + self.section.layout().spacing()
        # At most a third of the pane belongs to optional details. Also reserve
        # a preview header when wrapped commands/status need additional room.
        budget = max(0, min(self.pane.height() // 3, self.pane.height() - fixed - header - 36))
        body = self.content.layout()
        width = max(1, self.scroll.viewport().width())
        wanted = body.totalHeightForWidth(width) if body.hasHeightForWidth() else body.sizeHint().height()
        height = min(max(0, wanted), budget)
        if self.scroll.minimumHeight() != height or self.scroll.maximumHeight() != height:
            self.scroll.setFixedHeight(height)


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


def _clear_group_title(widget):
    widget.setTitle("")
    widget.setProperty("quickDisclosureContent", True)
    # The group can already have cached its styled title margins. Re-polish
    # the disclosure rule and notify QGroupBox to recompute its frame geometry.
    widget.style().unpolish(widget)
    widget.style().polish(widget)
    QApplication.sendEvent(widget, QEvent(QEvent.Type.StyleChange))
    widget.updateGeometry()


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
        _clear_group_title(widget)
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
                        "_acquisition_mode": 180, "_point_step_mt": 120,
                        "_point_tolerance_mt": 120, "_point_hold_s": 120,
                        "point_step": 120, "point_tolerance": 120, "point_stable": 120,
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
                 "_acquisition_mode", "_point_step_mt", "_point_tolerance_mt", "_point_hold_s",
                 "point_step", "point_tolerance", "point_stable",
                 "_rotator", "_rotation_settle", "_angle_a", "_angle_b",
                 "start_field", "stop_field", "angles", "rotator",
                 "_center", "_exposure", "_frames",
                 "lf_center", "lf_exposure", "lf_frames"):
        widget = getattr(panel, name, None)
        if isinstance(widget, QWidget):
            widget.setProperty("quickLabelAbove", True)
    result = _fold_groups(panel, [
        ("APS100 — attoDRY1000", "Magnet connection"),
        ("attoDRY2100 connection and telemetry", "Magnet connection"),
        ("New gate rows", "Add gate conditions"),
        ("Gate run preview", "Gate plan details · selected condition"),
        ("Filename", "Output filename · preview and tokens"),
    ])
    # Batch entry is a primary gate-table action in both MCD workspaces.
    if result.get("New gate rows") is not None:
        entry = result["New gate rows"]
        entry.set_expanded(True)
        # The 1000 page wraps this editor beside a stretching spacer. Quick
        # forms have no native width hint, so give the editor that row's space.
        slot = _layout_slot(entry.parentWidget().layout(), entry)
        if slot is not None and isinstance(slot[0], QBoxLayout):
            row, index = slot
            row.setStretch(index, 1)
            for other in range(row.count()):
                if row.itemAt(other).spacerItem() is not None:
                    row.setStretch(other, 0)
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
    # Source pages retain the same full-width section order in both shells.
    for section in result.values():
        if section is not None:
            section.setProperty("quickSectionBoundary", True)
    scroll = getattr(panel, "_settings_scroll", getattr(panel, "_scroll", None))
    if scroll is not None:
        _compact_sections(scroll.widget())
        _workspace_surface(scroll.widget())
        scroll.widget().layout().setSpacing(6)
    _workspace_surface(panel._splitter.widget(1))
    # The 2100 condition table includes its input-mode selector and an index;
    # a little more width keeps every gate value on one inline band.
    panel._quick_pane_width = panel._mcd_pane_width
    return result


def _motion_layout(panel):
    result = {}
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
    _clear_group_title(panel._execution_order_group)

    sequence_panel = panel._tree.parentWidget()
    if hasattr(panel._tree, "set_embedded"):
        panel._tree.set_embedded(True)
    panel._sequence_toggle.setProperty("quickDisclosure", True)
    panel._filename_details_toggle.setProperty("quickDisclosure", True)

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
    panel._sequence_toggle.toggled.connect(fit_execution)
    # Scalar headers can wrap after their QML form has been laid out. Refit
    # the fixed-height splitter when that measured content height changes too.
    panel._quick_preview_height_timer = QTimer(panel)
    panel._quick_preview_height_timer.setSingleShot(True)
    panel._quick_preview_height_timer.timeout.connect(fit_execution)
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
    _clear_group_title(filename_group)
    filename_group.setObjectName("QuickDualGateFilenameContent")
    filename_group.layout().setContentsMargins(0, 0, 0, 0)
    layout.insertWidget(0, filename)

    # QuickWorkbench mounts compact Apply/Run/Stop below the right scroll area.
    # Keep optional run tools beside those actions, with their own bounded
    # upward-opening body so progress and Stop remain visible on laptops.
    results_layout = panel._results_pane.layout()
    footer = results_layout.itemAt(1).widget()
    results_layout.removeWidget(footer)
    footer.layout().removeWidget(panel._readiness_lbl)
    layout.insertWidget(0, panel._readiness_lbl)
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
    details_scroll = QScrollArea()
    details_scroll.setObjectName("QuickDualGateRunDetailsScroll")
    details_scroll.setFrameShape(QFrame.Shape.NoFrame)
    details_scroll.setWidgetResizable(True)
    details_scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
    details_scroll.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)
    details_scroll.setMinimumHeight(0)
    details_scroll.setWidget(footer)
    run_details = DisclosureSection("Run details", details_scroll, expanded=False)
    run_details.setObjectName("QuickDualGateRunDetailsDisclosure")
    run_details.layout().removeWidget(details_scroll)
    run_details.layout().insertWidget(0, details_scroll)
    results_layout.addWidget(run_details)
    results_layout.addWidget(run_status)
    panel._quick_run_details_height = _RunDetailsHeight(
        panel._results_pane, panel._results_scroll, run_details, details_scroll, footer)
    panel._quick_safety_disclosure = safety
    panel._quick_execution_disclosure = execution
    panel._quick_filename_disclosure = filename
    panel._quick_run_details_disclosure = run_details
    panel._quick_run_details_scroll = details_scroll
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


def _hide_global_sample_field(panel, page_id):
    """Keep shared sample sources alive for filenames and session restoration."""
    if not panel.property("quickGlobalSample"):
        return
    attribute = {"dual_gate": "_sample_edit", "mega_sweep": "_sample_edit",
                 "power_sweep": "_devid_edit", "bfp": "_dev_edit",
                 "mcd": "_sample_id", "mcd2100": "_sample_id"}.get(page_id)
    editor = getattr(panel, attribute, None) if attribute else None
    if not isinstance(editor, QWidget):
        return
    editor.hide()
    # Semantic grouping has finished; remove the label in its final location.
    for label in panel.findChildren(QLabel):
        if label.buddy() is editor:
            label.hide()
    for form in panel.findChildren(QFormLayout):
        label = form.labelForField(editor)
        if label is not None:
            label.hide()
            # MCD identity fields each own a one-field column. Hiding only the
            # editor leaves that empty column occupying half the section.
            if page_id in {"mcd", "mcd2100"} and form.rowCount() == 1:
                owner = form.parentWidget()
                owner.hide()
                parent_grid = owner.parentWidget().layout()
                if isinstance(parent_grid, QGridLayout):
                    position = parent_grid.indexOf(owner)
                    if position >= 0:
                        _, column, _, _ = parent_grid.getItemPosition(position)
                        parent_grid.setColumnStretch(column, 0)
                        parent_grid.setColumnMinimumWidth(column, 0)
    for grid in panel.findChildren(QGridLayout):
        index = grid.indexOf(editor)
        if index < 0:
            continue
        row, column, _, _ = grid.getItemPosition(index)
        before = grid.itemAtPosition(row, column - 1) if column else None
        label = before.widget() if before is not None else None
        if isinstance(label, QLabel) and label.text().rstrip(":").strip().lower() == "sample id":
            label.hide()
    sections = getattr(panel, "_quick_gate_sections", {})
    if page_id == "dual_gate" and sections.get("sample") is not None:
        sections["sample"].setTitle("Location and temperature")
    if page_id in {"mcd", "mcd2100"}:
        for group in panel.findChildren(QGroupBox):
            if group.title() == "Sample / Device":
                group.setTitle("Location")


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
    _hide_global_sample_field(panel, page_id)
    panel._quick_workspace_layout = result
    return result
