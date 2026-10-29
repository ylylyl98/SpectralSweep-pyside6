"""Isolated Quick electrical layout checks; no controller or hardware imports."""
import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest
from PySide6.QtCore import QObject, Signal
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QWidget

from tests.quick_helpers import drain_qt_deletions, quick_application
from ui.instrument_panel import _ManualControlSection, _SMUSection
from ui.quick_instrument_electrical import configure_electrical_section
from ui.quick_parameter_forms import install_parameter_forms


class ElectricalState(QObject):
    connected = Signal(list)
    disconnected = Signal()
    error = Signal(str)
    readings_ready = Signal(object)
    manual_finished = Signal(str, str, float)
    manual_error = Signal(str)

    def __init__(self, connected=False):
        super().__init__()
        self.is_connected = connected
        self.calls = []

    def role_is_available(self, role):
        return self.is_connected

    def disconnect_instrument(self):
        self.is_connected = False
        self.disconnected.emit()

    def manual_control(self, *args, **kwargs):
        self.calls.append((args, kwargs))

    def connect_instrument(self, *args):
        self.calls.append(args)


@pytest.fixture
def cleanup():
    quick_application()
    owners = []
    yield owners
    for owner in owners:
        for form in getattr(owner, "_quick_parameter_forms", {}).get("forms", []):
            form.shutdown()
        owner.close()
        owner.deleteLater()
    drain_qt_deletions()


def _fields(coverage):
    return {source: field for form in coverage["forms"]
            for field, source in form.adapter._fields.items()}


def test_smu_keeps_address_mapping_and_limits_in_distinct_forms(cleanup):
    ctrl = ElectricalState()
    section = _SMUSection(ctrl)
    cleanup.append(section)
    controls = [section._connect_btn, section._disconnect_btn, section._termination]
    controls += list(section._role_combos.values())
    controls += list(section._curr_comp_by_role.values())
    states = {control: (control.isHidden(), control.isEnabled()) for control in controls}
    limit_layouts = {role: control.parentWidget().layout()
                     for role, control in section._curr_comp_by_role.items()}
    groups = configure_electrical_section(section)
    assert configure_electrical_section(section) is groups
    assert states == {control: (control.isHidden(), control.isEnabled()) for control in controls}
    assert section._limits_group is groups["limits"]
    for role, original in limit_layouts.items():
        assert groups[f"limits_{role}"].layout() is original
    coverage = install_parameter_forms(section, "instruments")
    fields = _fields(coverage)
    connection = next(form for form in coverage["forms"]
                      if section._connect_btn in form.adapter._fields.values())
    assert set(section._role_combos.values()).issubset(connection.adapter._fields.values())
    assert not set(section._curr_comp_by_role.values()).intersection(connection.adapter._fields.values())
    for role, control in section._curr_comp_by_role.items():
        form = next(form for form in coverage["forms"] if control in form.adapter._fields.values())
        assert section._volt_comp_by_role[role] in form.adapter._fields.values()
        assert sum(source in form.adapter._fields.values()
                   for source in section._curr_comp_by_role.values()) == 1
    # The real connection validation still rejects duplicate role addresses.
    for combo in (section._role_vbg, section._role_vtg):
        combo.addItem("GPIB0::1::INSTR")
        combo.setCurrentText("GPIB0::1::INSTR")
    connection.adapter.refresh()
    assert fields[section._connect_btn].trigger()
    assert ctrl.calls == []
    assert "different" in section._status.text()


@pytest.mark.parametrize("width", [350, 460])
def test_manual_channels_keep_rows_and_original_safe_commands(width, cleanup):
    ctrl = ElectricalState(connected=True)
    section = _ManualControlSection(ctrl)
    cleanup.append(section)
    original_rows = {role: [section._target_spn[role].parentWidget().layout().itemAt(i).layout()
                            for i in range(3)] for role in section._CHANNELS}
    groups = configure_electrical_section(section)
    coverage = install_parameter_forms(section, "instruments")
    section.resize(width, 900)
    section.show()
    QTest.qWait(30)
    fields = _fields(coverage)
    for role in section._CHANNELS:
        form = next(form for form in coverage["forms"]
                    if section._target_spn[role] in form.adapter._fields.values())
        sources = list(form.adapter._fields.values())
        assert section._voltage_lbl[role] in sources
        assert section._current_lbl[role] in sources
        assert section._ramp_btn_by_role[role] in sources
        assert section._zero_btn_by_role[role] in sources
        assert sum(source in sources for source in section._target_spn.values()) == 1
        assert len(form.adapter.model.bands) == 3
        # Existing source row layouts still own the fields after migration.
        assert all(row.parent() is not None for row in original_rows[role])
        assert form.width() <= width
        assert groups[f"channel_{role}"].property("quickSectionBoundary")
    assert fields[section._target_spn["Vbg"]].commit("1.25")
    assert fields[section._ramp_btn_by_role["Vbg"]].trigger()
    assert ctrl.calls[-1][0] == ("ramp_to", "Vbg", 1.25)
    assert not fields[section._zero_all_btn].trigger()
    assert len(ctrl.calls) == 1
    ctrl.manual_finished.emit("ramp_to", "Vbg", 1.25)
    for form in coverage["forms"]:
        form.adapter.refresh()
    assert fields[section._zero_all_btn].trigger()
    assert ctrl.calls[-1][0] == ("zero_all", "", 0.0)
    assert "ramp_step_V" in ctrl.calls[-1][1]
    assert "delay_s" in ctrl.calls[-1][1]


def test_unknown_section_has_no_layout_side_effects(cleanup):
    section = QWidget()
    cleanup.append(section)
    assert configure_electrical_section(section) is None
    assert not hasattr(section, "_quick_electrical_layout")
