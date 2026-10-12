"""Sample editable setup sequences, configured through session restoration."""
import pandas as pd

from ui import presets_panel as panel


def set_paired_setup_draft(widget):
    definition = pd.DataFrame([
        dict(Enable=True, Parameter=parameter, Values=values, Group=1)
        for parameter, values in [
            ('Measurement setup', 'PIXIS, WinSpec'),
            ('Center Wavelength (nm)', '720, 1100'),
            ('Exposure Time (ms)', '100, 100'),
            ('Accumulations (EPF)', '1, 1'),
        ]
    ])
    state = widget.capture_session_state()
    state.update(
        draft_loop=definition.to_dict('records'),
        loop_mode='Customized',
        execution_order=panel._default_execution_order(definition, 'Customized'),
        nested_schedule_enabled=True,
        loop_run_selections=[],
    )
    widget.restore_session_state(state)
