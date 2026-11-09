import os
os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
os.environ.setdefault('QT_QUICK_BACKEND', 'software')
from tests.quick_helpers import quick_application, drain_qt_deletions
from ui.power_sweep_panel import PowerSweepPanel
from ui.quick_parameter_forms import install_parameter_forms
from ui.quick_workspace_layouts import configure_workspace_layout


def test_motion_disclosures_are_independent_of_condition_enable():
    app = quick_application()
    previous = app.property('spectralQuick')
    app.setProperty('spectralQuick', True)
    panel = PowerSweepPanel()
    configure_workspace_layout(panel, 'power_sweep')
    coverage = install_parameter_forms(panel, 'power_sweep')
    try:
        fields = {source: field for form in coverage['forms'] for field, source in form.adapter._fields.items()}
        for button in (panel._conditions_section.toggle, panel._cal_grp, panel._hardware_grp,
                       panel._condition_editor._advanced_group):
            assert fields[button].kind == 'disclosure'
        panel._conditions_section.set_expanded(True)
        panel._conditions_grp.setChecked(True)
        panel._conditions_section.set_expanded(False)
        assert panel._conditions_grp.isChecked()
        assert panel.capture_session_state()['conditions_enabled']
        panel._conditions_section.set_expanded(True)
        panel._conditions_grp.setChecked(False)
        assert panel._conditions_section.toggle.isChecked()
        assert not panel._condition_editor.isEnabled()
    finally:
        for form in coverage['forms']: form.shutdown()
        panel.shutdown(timeout_ms=0); panel.close(); panel.deleteLater(); drain_qt_deletions()
        app.setProperty('spectralQuick', previous)
