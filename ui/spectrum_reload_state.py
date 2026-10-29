"""In-memory Spectrum state transfer. No instrument calls or disk persistence."""
from copy import deepcopy

from PySide6.QtCore import QSignalBlocker


_DATA_FIELDS = (
    "_display_backend", "_backend_profiles", "_connected", "_supports_2d",
    "_action_controls_requested", "_continuous_frames", "_continuous_started_at",
    "_last_data_kind", "_last_data", "_last_wavelength",
    "_acquisition_settings_snapshot", "_pending_acquisition_snapshot",
    "_last_acquisition_snapshot", "_active_setup_key", "_setup_views",
    "_spectrum_data", "_frame_data", "_metadata_context", "_observed_readbacks",
    "_reference_sample_id", "_unavailable_references",
    "_optics_snapshot", "_pre_local_ranges", "_pre_local_recipe",
    "_latest_winspec_frame", "_winspec_frame_context", "_winspec_frame_datatype",
    "_winspec_temperature_guard", "_winspec_native_capture",
    "_winspec_temperature_verification", "_winspec_intensity_processing",
    "_winspec_raw_accumulated_counts", "_winspec_start_acceleration", "_winspec_capture_timing",
)
_LABELS = (
    "_status_lbl", "_rate_lbl", "_source_label", "_optics_readback",
    "_calibration_status", "_winspec_temperature_label", "_frames_label", "_frame_help",
)


def capture_spectrum_state(panel):
    from ui.quick_editing import require_committed_quick_edits
    require_committed_quick_edits(panel)
    references = []
    for ref in panel._references:
        record = {k: v for k, v in ref.items() if not k.startswith("_row_")}
        # An edit need not have lost focus when the keyboard shortcut is used.
        record["name"] = ref["_row_name"].text().strip() or ref["name"]
        references.append(record)
    return deepcopy({
        "session": panel.capture_session_state(),
        "data": {name: getattr(panel, name) for name in _DATA_FIELDS if hasattr(panel, name)},
        "references": references,
        "selected_row": panel._reference_list.currentRow(),
        "scroll": panel._reference_list.verticalScrollBar().value(),
        "plot": panel._spec_plot.capture_view(),
        "axis_unit": panel._spec_plot.axis_unit,
        "frame_view": panel._frame_plot._view.getView().getState(),
        "frame_levels": panel._frame_plot._view.getLevels(),
        "splitter": panel._content_splitter.saveState(),
        "andor": panel._andor_controls.capture_reload_state(),
        "andor_expanded": panel._andor_toggle.isChecked(),
        "calibration_enabled": panel._use_winspec_nm.isChecked(),
        "reverse_pixels": panel._reverse_winspec_pixels.isChecked(),
        "ranges": [(w.minimum(), w.maximum(), w.decimals() if hasattr(w, "decimals") else None)
                   for w in (panel._center, panel._exposure, panel._accumulations)],
        "grating": panel._grating.currentIndex(),
        "output_port": panel._output_port.currentIndex(),
        "labels": {name: (getattr(panel, name).text(), getattr(panel, name).toolTip()) for name in _LABELS},
        "error": panel._error_details.toPlainText(),
        "error_visible": not panel._error_panel.isHidden(),
    })


def restore_spectrum_state(panel, state):
    for name, value in state["data"].items():
        setattr(panel, name, value)
    panel._andor_controls.restore_reload_state(state["andor"])
    if panel._connected:
        panel._update_device_status()
    if hasattr(panel, "_optics_snapshot"):
        panel._on_optics_status(panel._optics_snapshot)
    for widget, (low, high, decimals) in zip(
            (panel._center, panel._exposure, panel._accumulations), state["ranges"]):
        with QSignalBlocker(widget):
            if decimals is not None:
                widget.setDecimals(decimals)
            widget.setRange(low, high)
    panel.restore_session_state(state["session"])
    panel._use_winspec_nm.setChecked(state["calibration_enabled"])
    panel._reverse_winspec_pixels.setChecked(state["reverse_pixels"])
    panel._grating.setCurrentIndex(state["grating"])
    panel._output_port.setCurrentIndex(state["output_port"])
    if panel._spectrum_data is not None:
        wl, counts, snapshot = panel._spectrum_data
        panel._set_spectrum_axis(snapshot)
        panel._spec_plot.update_spectrum(wl, panel._normalized_counts(counts))
    else:
        panel._set_spectrum_axis({"instrument_identity": {"axis_unit": state["axis_unit"]}})
    if panel._frame_data is not None:
        panel._frame_plot.update_frame(panel._frame_data[0])
        panel._frame_plot._view.setLevels(*state["frame_levels"])
        panel._frame_plot._view.getView().setState(state["frame_view"])
    panel._references = state["references"]
    for ref in panel._references:
        panel._add_reference_item(ref)
        ref["_row_checkbox"].setChecked(ref["visible"])
    panel._refresh_reference_curves()
    panel._refresh_comparison()
    panel._reference_list.setCurrentRow(state["selected_row"])
    panel._reference_list.verticalScrollBar().setValue(state["scroll"])
    panel._spec_plot.restore_view(state["plot"])
    panel._content_splitter.restoreState(state["splitter"])
    panel._save_btn.setEnabled(panel._last_data is not None)
    panel._retry_reference_btn.setEnabled(any(not r["saved"] for r in panel._references))
    panel._andor_toggle.setChecked(state["andor_expanded"])
    is_andor = panel._connected and (getattr(panel._ctrl, "identity", {}) or {}).get("backend") == "andor_sdk2"
    panel._andor_toggle.setVisible(is_andor)
    panel._andor_controls.setVisible(is_andor and state["andor_expanded"])
    panel._refresh_action_controls()
    for name, (text, tooltip) in state["labels"].items():
        getattr(panel, name).setText(text)
        getattr(panel, name).setToolTip(tooltip)
    panel._error_details.setPlainText(state["error"])
    panel._error_panel.setVisible(state["error_visible"])


def spectrum_reload_block_reason(panel):
    if (panel._continuous_mode is not None or panel._pending_acquisition is not None
            or panel._abort_btn.isEnabled() or panel._add_pending
            or (panel._connected and not panel._action_controls_requested)):
        return "Wait for Spectrum acquisition/settings to finish before reloading."
    controller = panel._ctrl
    if any(bool(getattr(controller, name, False)) for name in
           ("is_busy", "switching_locked", "spectrum_actions_blocked")):
        return "Wait for the instrument operation to finish before reloading."
    page = panel._wavelength_dialog
    broad = getattr(page, "broad_dialog", None)
    if (panel._calibration_capture_pending or panel._calibration_previous_nm is not None
            or getattr(panel, "_calibration_grating_pending", None) is not None
            or any(bool(getattr(broad, name, False)) for name in
                   ("batch_active", "sweep_active", "analysis_running"))):
        return "Wait for calibration acquisition/analysis to finish before reloading."
    return ""
