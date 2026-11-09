import os
from types import SimpleNamespace
import pytest
os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
os.environ.setdefault('QT_QUICK_BACKEND', 'software')
from PySide6.QtCore import Qt
from tests.quick_helpers import quick_application, drain_qt_deletions
from ui.quick_workspace_layouts import configure_workspace_layout
from ui.quick_parameter_forms import install_parameter_forms
from ui.workflow_sections import disclosure_toggle


def test_toggle_arrow_tracks_checked_state():
    quick_application()
    button = disclosure_toggle('Log')
    assert button.arrowType() == Qt.ArrowType.RightArrow
    button.setChecked(True)
    assert button.arrowType() == Qt.ArrowType.DownArrow


@pytest.mark.parametrize('page', ['mcd', 'mcd2100'])
def test_mcd_log_and_spectrum_quick_disclosures_preserve_state(page):
    app = quick_application(); previous = app.property('spectralQuick')
    app.setProperty('spectralQuick', True)
    if page == 'mcd':
        from ui.mcd_panel import MCDPanel
        from ui.quick_workspaces_preview import DisconnectedPreviewMagnet
        panel = MCDPanel(DisconnectedPreviewMagnet(), None, None, None)
    else:
        from ui.mcd2100_panel import MCD2100Panel
        panel = MCD2100Panel(SimpleNamespace(state=SimpleNamespace(name='DISCONNECTED'), generation=0))
    configure_workspace_layout(panel, page)
    coverage = install_parameter_forms(panel, page)
    try:
        fields = {source: field for form in coverage['forms'] for field, source in form.adapter._fields.items()}
        controls = [panel._show_log_chk]
        if page == 'mcd': controls.append(panel._show_spectrum_chk)
        for control in controls:
            assert fields[control].kind == 'disclosure'
            old = control.isChecked()
            control.setChecked(not old)
            assert control.arrowType() == (Qt.ArrowType.DownArrow if not old else Qt.ArrowType.RightArrow)
        saved = panel.capture_session_state()
        panel._show_log_chk.setChecked(not panel._show_log_chk.isChecked())
        panel.restore_session_state(saved)
        assert panel._show_log_chk.isChecked() == saved['log_visible']
    finally:
        for form in coverage['forms']: form.shutdown()
        panel.shutdown(timeout_ms=0); panel.close(); panel.deleteLater(); drain_qt_deletions()
        app.setProperty('spectralQuick', previous)
