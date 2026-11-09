import os
from types import SimpleNamespace
os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
os.environ.setdefault('QT_QUICK_BACKEND', 'software')
from tests.quick_helpers import quick_application, drain_qt_deletions
from ui.mcd2100_panel import MCD2100Panel
from ui.quick_workspace_layouts import configure_workspace_layout
from ui.quick_parameter_forms import install_parameter_forms


def test_connection_sections_and_mode_survive_quick_adapter(monkeypatch):
    app = quick_application()
    previous = app.property('spectralQuick')
    app.setProperty('spectralQuick', True)
    panel = MCD2100Panel(SimpleNamespace(state=SimpleNamespace(name='DISCONNECTED'), generation=0))
    sections = configure_workspace_layout(panel, 'mcd2100')
    coverage = install_parameter_forms(panel, 'mcd2100')
    try:
        fields = {source: field for form in coverage['forms'] for field, source in form.adapter._fields.items()}
        assert sections['attoDRY2100 connection and telemetry'].toggle.text() == 'Magnet connection'
        assert fields[sections['attoDRY2100 connection and telemetry'].toggle].kind == 'disclosure'
        assert fields[panel._connection_advanced.toggle].kind == 'disclosure'
        assert fields[panel._temperature_diagnostics.toggle].kind == 'disclosure'
        assert panel.field_value.parentWidget().parentWidget().objectName() == 'Mcd2100LiveStatus'
        panel._acquisition_mode.setCurrentIndex(1)
        assert panel.point_pairing.isChecked()
        state = panel.capture_session_state()
        panel._acquisition_mode.setCurrentIndex(0)
        panel.restore_session_state(state)
        assert panel._acquisition_mode.currentData() == 'point_pairing'
        assert panel.point_pairing.isChecked()
    finally:
        for form in coverage['forms']: form.shutdown()
        panel.shutdown(timeout_ms=0)
        panel.close(); panel.deleteLater(); drain_qt_deletions()
        app.setProperty('spectralQuick', previous)
