"""Idle Dual Gate presentation transfer; no instrument calls or persistence."""
from copy import deepcopy
from math import isnan

from PySide6.QtCore import Qt, QSignalBlocker, QItemSelectionModel
from PySide6.QtWidgets import (
    QApplication, QAbstractItemView, QAbstractButton, QComboBox, QDoubleSpinBox,
    QSpinBox, QLineEdit,
)


_DATA_FIELDS = (
    "_loop_src", "_batch_src", "_applied_loop_run_selections", "_applied_mode",
    "_applied_acquisition_grouping", "_applied_execution_order", "_execution_order",
    "_nested_schedule_enabled", "_legacy_loop_order", "_applied_legacy_loop_order",
    "_tables_dirty", "_last_power_uw", "_batch_row_clipboard", "_batch_history",
    "_batch_history_index", "_last_acquisition_ref", "_hardware_incident_active",
    "_final_seq", "_df_batch", "_acquisition_schedule", "_total_acq", "_total_points",
    "_done_acq", "_done_frames", "_current_seq_i", "_current_label", "_current_rep_i",
    "_current_frame_i", "_current_frame_total", "_run_outcome", "_manual_filename_parts",
    "_applied_plan_details",
)


def dual_gate_reload_block_reason(panel):
    if getattr(panel, "plan_build_running", lambda: False)():
        return "Wait for the Dual Gate sequence build to finish before reloading."
    thread = panel._run_thread
    if (thread is not None and thread.isRunning()) or panel.metadata_finalizing or panel._stop_btn.isEnabled():
        return "Wait for Dual Gate acquisition and saving to finish before reloading."
    for controller in (panel._lf6, panel._smu, panel._rot, panel._stage, panel._pm):
        if any(bool(getattr(controller, field, False)) for field in
               ("is_busy", "switching_locked", "spectrum_actions_blocked")):
            return "Wait for the instrument operation to finish before reloading Dual Gate."
    dialog = getattr(panel._tree, "_full_sequence_dialog", None)
    if dialog is not None and dialog.isVisible():
        return "Close the full measurement sequence window before reloading Dual Gate."
    return ""


def _controls(owner):
    values = {}
    for name, widget in vars(owner).items():
        if isinstance(widget, QLineEdit):
            values[name] = ("text", widget.text())
        elif isinstance(widget, (QDoubleSpinBox, QSpinBox)):
            values[name] = ("value", widget.value())
        elif isinstance(widget, QComboBox):
            values[name] = ("combo", widget.currentText())
        elif isinstance(widget, QAbstractButton) and widget.isCheckable():
            values[name] = ("checked", widget.isChecked())
    return values


def _restore_controls(owner, values):
    setters = {"text": "setText", "value": "setValue", "checked": "setChecked"}
    for name, (kind, value) in values.items():
        widget = getattr(owner, name)
        with QSignalBlocker(widget):
            if kind == "combo":
                index = widget.findText(value)
                if index < 0:
                    raise RuntimeError(f"The new page no longer supports the selected value for {name}.")
                widget.setCurrentIndex(index)
            else:
                getattr(widget, setters[kind])(value)


def capture_dual_gate_state(panel):
    from ui.quick_editing import require_committed_quick_edits
    require_committed_quick_edits(panel)
    # A shortcut can run before a table delegate commits its current text.
    focused = QApplication.focusWidget()
    for table in (panel._loop_table, panel._batch_table):
        if focused is not None and table.state() == QAbstractItemView.State.EditingState and table.isAncestorOf(focused):
            editor = focused
            while editor.parentWidget() is not table.viewport():
                editor = editor.parentWidget()
                if editor is None:
                    break
            if editor is not None:
                table.commitData(editor)
    tables = {}
    for name in ("_loop_table", "_batch_table", "_execution_order_table"):
        table = getattr(panel, name, None)
        if table is not None:
            tables[name] = {
                "selected": [(i.row(), i.column()) for i in table.selectedIndexes()],
                "current": (table.currentRow(), table.currentColumn()),
                "scroll": (table.horizontalScrollBar().value(), table.verticalScrollBar().value()),
            }
    return deepcopy({
        "session": panel.capture_session_state(),
        "data": {name: getattr(panel, name) for name in _DATA_FIELDS if hasattr(panel, name)},
        "controls": _controls(panel), "calculator_controls": _controls(panel._sweep_calc),
        "calculator_data": {name: getattr(panel._sweep_calc, name) for name in
                            ("_label_auto", "_body_visible", "_calculated_rows")},
        "tree_plan": panel._tree._last_plan,
        "tree_expansion": _tree_expansion(panel._tree) if not hasattr(panel._tree, "capture_view_state") else [],
        "tree_scroll": panel._tree.verticalScrollBar().value() if not hasattr(panel._tree, "capture_view_state") else 0,
        "sequence_view": panel._tree.capture_view_state() if hasattr(panel._tree, "capture_view_state") else None,
        "tables": tables, "log": panel._log_text.toHtml(),
        "log_scroll": panel._log_text.verticalScrollBar().value(),
        "status": (panel._status_lbl.text(), {
            bytes(name).decode(): panel._status_lbl.property(bytes(name).decode())
            for name in panel._status_lbl.dynamicPropertyNames()
            # Qt/PySide add private pointer-valued properties when Quick
            # adapters subscribe. Those are not portable presentation state.
            if not bytes(name).startswith(b"_")
        }),
        "progress": (panel._progress.minimum(), panel._progress.maximum(), panel._progress.value()),
        "run_time_text": panel._run_time_lbl.text(),
        "scrolls": {name: getattr(panel, name).verticalScrollBar().value()
                    for name in ("_workflow_scroll", "_results_scroll")},
    })


def _tree_expansion(tree):
    result = []
    def visit(item, path):
        result.append((path, item.isExpanded()))
        for index in range(item.childCount()):
            visit(item.child(index), (*path, index))
    for index in range(tree.topLevelItemCount()):
        visit(tree.topLevelItem(index), (index,))
    return result


def _same_plan_values(left, right):
    """Unset optional voltages use NaN, whose ordinary equality is false."""
    if isinstance(left, dict) and isinstance(right, dict):
        return left.keys() == right.keys() and all(_same_plan_values(value, right[key]) for key, value in left.items())
    if isinstance(left, (list, tuple)) and isinstance(right, (list, tuple)):
        return len(left) == len(right) and all(_same_plan_values(a, b) for a, b in zip(left, right))
    if isinstance(left, float) and isinstance(right, float) and isnan(left) and isnan(right):
        return True
    return left == right


def restore_dual_gate_state(panel, state):
    # Safety preferences may have been changed on another page. Restoring this
    # page's editor must not write its older value back to the shared config.
    with QSignalBlocker(panel._safe_jump_spin):
        panel.restore_session_state(state["session"])
    saved = state["data"]
    differences = [name for name, differs in (
        ("contexts", not _same_plan_values(panel._final_seq, saved["_final_seq"])),
        ("batch", not panel._df_batch.equals(saved["_df_batch"])),
        ("schedule", not _same_plan_values(panel._acquisition_schedule, saved["_acquisition_schedule"])),
    ) if differs]
    if differences:
        raise RuntimeError("The applied Dual Gate plan changes under this source (" + ", ".join(differences) + "); keep the current page and restart before using the new planner.")
    # Session restoration visits default rows before replacing the tables.
    # Discard those transient membership records and rebuild only saved rows.
    panel._loop_run_selections = type(panel._loop_run_selections)(state["session"]["loop_run_selections"])
    panel._loop_selection_sync_key = None
    # The applied plan and completed-run position are runtime state, separate
    # from the editable draft and from a newly rebuilt default plan.
    for name, value in state["data"].items():
        setattr(panel, name, value)
    _restore_controls(panel, state["controls"])
    _restore_controls(panel._sweep_calc, state["calculator_controls"])
    for name, value in state["calculator_data"].items():
        setattr(panel._sweep_calc, name, value)
    panel._sweep_calc._calc_timer.stop()
    panel._preview_timer.stop()
    panel._refresh_draft_state()
    panel._refresh_filename_preview()
    panel._update_batch_action_states()
    if state["tree_plan"]:
        panel._tree.update_plan(**state["tree_plan"])
    if state.get("sequence_view") is not None and hasattr(panel._tree, "restore_view_state"):
        panel._tree.restore_view_state(state["sequence_view"])
    elif hasattr(panel._tree, "topLevelItem"):
        for path, expanded in state["tree_expansion"]:
            item = panel._tree.topLevelItem(path[0])
            for index in path[1:]:
                item = item.child(index) if item is not None else None
            if item is not None:
                item.setExpanded(expanded)
        panel._tree.verticalScrollBar().setValue(state["tree_scroll"])
    for name, saved in state["tables"].items():
        table = getattr(panel, name)
        with QSignalBlocker(table):
            table.clearSelection()
            table.setCurrentCell(*saved["current"], QItemSelectionModel.SelectionFlag.NoUpdate)
            for row, column in saved["selected"]:
                table.selectionModel().select(table.model().index(row, column), QItemSelectionModel.SelectionFlag.Select)
            table.horizontalScrollBar().setValue(saved["scroll"][0])
            table.verticalScrollBar().setValue(saved["scroll"][1])
    panel._log_text.setHtml(state["log"])
    panel._log_text.verticalScrollBar().setValue(state["log_scroll"])
    panel._status_lbl.setText(state["status"][0])
    for name, value in state["status"][1].items():
        panel._status_lbl.setProperty(name, value)
    panel._status_lbl.style().unpolish(panel._status_lbl)
    panel._status_lbl.style().polish(panel._status_lbl)
    panel._progress.setRange(*state["progress"][:2])
    panel._progress.setValue(state["progress"][2])
    panel._run_time_lbl.setText(state.get("run_time_text", "Elapsed 00:00 · Remaining —"))
    panel._filename_details.setVisible(panel._filename_details_toggle.isChecked())
    for toggle in (panel._filename_details_toggle, panel._log_toggle):
        toggle.setArrowType(Qt.ArrowType.DownArrow if toggle.isChecked() else Qt.ArrowType.RightArrow)
    # The log container is the direct parent installed by the page layout.
    panel._log_text.parentWidget().setVisible(panel._log_toggle.isChecked())
    for name, value in state["scrolls"].items():
        getattr(panel, name).verticalScrollBar().setValue(value)


def detach_dual_gate_windows(panel):
    viewer = panel._spectrum_viewer
    if viewer is None:
        return None
    saved = {
        "viewer": viewer, "flags": viewer.windowFlags(), "geometry": viewer.saveGeometry(),
        "visible": viewer.isVisible(), "payload": viewer._last_payload,
        "plot_view": viewer._plot.getViewBox().getState(),
        "image_view": viewer._image.getView().getViewBox().getState(), "levels": viewer._image.getLevels(),
        "message": viewer._info.text(),
    }
    panel._viewer_connections.clear()
    panel._spectrum_viewer = None
    viewer.setParent(None)
    return saved


def attach_dual_gate_windows(panel, saved):
    if saved is None:
        return
    viewer, visible = saved["viewer"], saved["visible"]
    panel._spectrum_viewer = viewer
    viewer.setParent(panel, saved["flags"])
    viewer.restoreGeometry(saved["geometry"])
    panel._viewer_connections.connect(viewer.visibility_changed, panel._on_spectrum_viewer_visibility_changed)
    viewer.setVisible(visible)
    if saved["payload"] is not None:
        viewer.set_acquisition(saved["payload"])
        viewer._plot.getViewBox().setState(saved["plot_view"])
        viewer._image.getView().getViewBox().setState(saved["image_view"])
        if saved["levels"] is not None:
            viewer._image.setLevels(*saved["levels"])
    else:
        viewer.show_message(saved["message"])
    panel._on_spectrum_viewer_visibility_changed(visible)
