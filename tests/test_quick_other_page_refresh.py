"""Spectrum and Settings refresh through the real Quick transaction, without devices."""
import sys

import numpy as np
import pytest
from PySide6.QtCore import QSignalBlocker, SIGNAL
from PySide6.QtTest import QTest

from tests.test_quick_workbench import app, workbench
from tests.test_quick_page_refresh import quick_mode, refresh_shell, source_override
from tests.quick_helpers import drain_qt_deletions
from ui import page_reload
from ui.refresh_manifest import QUICK_PAGE_MODULES
from ui.settings_panel import SettingsPanel


@pytest.fixture
def other_page_shell(refresh_shell, request):
    quick, window = refresh_shell
    page_id = request.param
    if page_id == "settings":
        old = window._settings
        panel = SettingsPanel(lf6_ctrl=None, bind_controller=False)
        index = window._tabs.indexOf(old)
        window._tabs.insertTab(index, panel, "Settings")
        window._tabs.removeTab(window._tabs.indexOf(old))
        window._tab_ids.pop(old)
        window._tab_ids[panel] = "settings"
        window._settings = panel
        window._session_panels["settings"] = panel
        # Match the transaction's blocked tab notifications: publishing a tab
        # before rebind would eagerly build editors and immediately retire them.
        with QSignalBlocker(window._tabs):
            window._tabs.setCurrentWidget(panel)
        quick.rebind_page("settings", old, panel)
    else:
        window._tabs.setCurrentWidget(window._spectrum)
    quick.refresh()
    QTest.qWait(60)
    yield quick, window
    window._spectrum.unbind_controller_signals()


@pytest.mark.parametrize("other_page_shell", ["spectrum"], indirect=True)
def test_spectrum_refresh_loads_acquisition_helper_and_keeps_data_and_controller(
        other_page_shell, monkeypatch, tmp_path):
    quick, window = other_page_shell
    old, controller = window._spectrum, window._lf6
    old_command = quick._commands["spectrum"]
    previous_engine = quick.navigation.engine()
    expected_receivers = controller.receivers(SIGNAL("spectrum_ready(PyObject,PyObject)"))
    assert expected_receivers > 0
    for control, value in ((old._exposure, 345.0), (old._center, 765.0), (old._accumulations, 7)):
        with QSignalBlocker(control):
            control.setValue(value)
    x, y = np.arange(10.) + 600, np.arange(10.) + 1
    snapshot = {"instrument_identity": {"backend": "lightfield", "axis_unit": "nm"},
                "sample_id": "in-memory-sample"}
    old.push_spectrum(x, y, snapshot)
    reference = old.add_reference(x, y, auto_save=False)
    reference["_row_name"].setText("reference draft")
    reference["_row_checkbox"].setChecked(False)
    old._reference_list.setCurrentRow(0)
    source_override(monkeypatch, tmp_path, "ui.quick_acquisition_sections",
                    lambda text: text.replace('"Spectrum display"', '"Updated spectrum display"'))

    new = page_reload.reload_page(window, "spectrum")
    QTest.qWait(80)
    assert new is not old and new._ctrl is controller is window._lf6
    assert new._quick_acquisition_sections["display"].title() == "Updated spectrum display"
    assert (new._exposure.value(), new._center.value(), new._accumulations.value()) == (345, 765, 7)
    np.testing.assert_array_equal(new._spectrum_data[0], x)
    np.testing.assert_array_equal(new._spectrum_data[1], y)
    assert new.references[0]["id"] == reference["id"]
    assert new.references[0]["name"] == "reference draft"
    assert not new.references[0]["visible"] and not new.references[0]["saved"]
    assert new._reference_list.currentRow() == 0
    assert window._session_panels["spectrum"] is new
    assert window._tabs.currentWidget() is new
    assert quick._page_editors["spectrum"][0] is new
    assert quick._page_editors["spectrum"][2]["references"].model.panel is new
    assert old_command.adapter._closed
    assert quick._commands["spectrum"].adapter.buttons["apply"] is new._apply_btn
    assert quick.navigation.engine() is previous_engine
    assert controller.receivers(SIGNAL("spectrum_ready(PyObject,PyObject)")) == expected_receivers
    assert controller.shutdown_calls == 0
    assert not controller._settings_pending and not controller.acquisition_in_progress


@pytest.mark.parametrize("other_page_shell", ["settings"], indirect=True)
def test_settings_refresh_loads_layout_helper_and_keeps_unsaved_drafts(
        other_page_shell, monkeypatch, tmp_path):
    quick, window = other_page_shell
    old = window._settings
    previous_engine, controller = quick.navigation.engine(), window._lf6
    previous_forms = quick._page_editors["settings"][2]["forms"]
    for control, value in ((old._exposure, 456.0), (old._center, 876.0), (old._accum, 9)):
        with QSignalBlocker(control):
            control.setValue(value)
    for control, value in ((old._base_out_edit, "unsaved-output-draft"),
                           (old._notifications.name_edit, "unsaved-PC-name")):
        with QSignalBlocker(control):
            control.setText(value)
    old._categories.setCurrentIndex(1)
    old._status_lbl.setText("Unsaved settings remain local")
    source_override(monkeypatch, tmp_path, "ui.quick_settings_layout",
                    lambda text: text.replace('"Acquisition defaults"', '"Updated acquisition defaults"'))

    new = page_reload.reload_page(window, "settings")
    QTest.qWait(80)
    assert new is not old and new._ctrl is old._ctrl is None
    assert window._lf6 is controller and controller.shutdown_calls == 0
    assert new._quick_settings_layout["acquisition"].title() == "Updated acquisition defaults"
    assert (new._exposure.value(), new._center.value(), new._accum.value()) == (456, 876, 9)
    assert new._base_out_edit.text() == "unsaved-output-draft"
    assert new._notifications.name_edit.text() == "unsaved-PC-name"
    assert new._categories.currentIndex() == 1
    assert new._status_lbl.text() == "Unsaved settings remain local"
    assert window._session_panels["settings"] is new
    assert window._tabs.currentWidget() is new
    assert quick._page_editors["settings"][0] is new
    assert all(form.adapter._closed for form in previous_forms)
    assert all(not form.adapter._closed for form in quick._page_editors["settings"][2]["forms"])
    assert quick.navigation.engine() is previous_engine
    assert not controller._settings_pending and not controller.acquisition_in_progress


@pytest.mark.parametrize("other_page_shell", ["spectrum", "settings"], indirect=True)
def test_failed_other_page_layout_keeps_previous_quick_interface(
        other_page_shell, monkeypatch, tmp_path):
    quick, window = other_page_shell
    page_id = window._active_tab_id()
    old = getattr(window, "_" + page_id)
    entry = quick._page_editors[page_id]
    assert all(not form.adapter._closed for form in entry[2]["forms"])
    command = quick._commands.get(page_id)
    modules = {name: sys.modules.get(name) for name in QUICK_PAGE_MODULES[page_id]}
    helper, target = (("ui.quick_acquisition_sections", '    view, view_group_layout = _section(')
                      if page_id == "spectrum" else
                      ("ui.quick_settings_layout", '    acquisition, form = _group('))
    source_override(monkeypatch, tmp_path, helper, lambda text: text.replace(
        target, '    raise RuntimeError("broken other page layout")\n' + target))

    with pytest.raises(RuntimeError, match="broken other page layout"):
        page_reload.reload_page(window, page_id)
    drain_qt_deletions()
    assert getattr(window, "_" + page_id) is old
    assert window._tabs.currentWidget() is old
    assert window._session_panels[page_id] is old
    assert quick._page_editors[page_id] is entry
    assert all(not form.adapter._closed for form in entry[2]["forms"])
    assert quick._commands.get(page_id) is command
    if command is not None:
        assert not command.adapter._closed
    assert all(sys.modules.get(name) is module for name, module in modules.items())
    assert window._lf6.shutdown_calls == 0
