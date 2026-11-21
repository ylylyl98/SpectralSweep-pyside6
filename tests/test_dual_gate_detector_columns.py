"""Loop table: one row per optical parameter, one value column per detector."""
import os

os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')

import pandas as pd
import pytest
from PySide6.QtWidgets import QApplication

from app.paired_spectra import PAIRED_STATION_ID
from ui import presets_panel as panel
from utils.config import cfg

ANDOR = panel._ANDOR_COLUMN


@pytest.fixture(scope='module', autouse=True)
def app():
    yield QApplication.instance() or QApplication([])


def paired_definition():
    # A saved recipe lists the Andor parameters after all PIXIS parameters.
    return pd.DataFrame([
        dict(Enable=True, Parameter=parameter, Values=values, Group=group)
        for parameter, values, group in [
            ('Measurement setup', 'PIXIS + Andor InGaAs', 1),
            ('Center Wavelength (nm)', '650', 1),
            ('Exposure Time (ms)', '50, 200', 1),
            ('Accumulations (EPF)', '1', 1),
            ('Andor Center Wavelength (nm)', '1100', 1),
            ('Andor Exposure Time (ms)', '1000', 1),
            ('Andor Accumulations (EPF)', '4', 1),
            ('Stage Position', '0, 1', 1),
        ]
    ])


@pytest.fixture
def widget(monkeypatch):
    monkeypatch.setattr(cfg.lf6, 'station_id', PAIRED_STATION_ID)
    result = panel.PresetsPanel()
    state = result.capture_session_state()
    state.update(draft_loop=paired_definition().to_dict('records'), loop_mode='Customized',
                 loop_run_selections=[])
    result.restore_session_state(state)
    yield result
    result.close()


def parameters(table):
    return [table.cellWidget(row, 1).currentData() for row in range(table.rowCount())]


def test_paired_recipe_uses_one_row_per_parameter_and_an_andor_column(widget):
    table = widget._loop_table
    assert parameters(table) == ['Measurement setup', 'Center Wavelength (nm)', 'Exposure Time (ms)',
                                 'Accumulations (EPF)', 'Stage Position']
    assert not table.isColumnHidden(ANDOR)
    assert table.horizontalHeaderItem(2).text() == 'PIXIS'
    assert table.horizontalHeaderItem(ANDOR).text() == 'Andor InGaAs'
    assert [table.item(row, ANDOR).text() for row in (1, 2, 3)] == ['1100', '1000', '4']
    header = table.horizontalHeader()
    assert header.visualIndex(ANDOR) == header.visualIndex(2) + 1


def test_table_reads_back_the_stored_one_record_per_detector_parameter(widget):
    frame = panel._normalize_loop(panel._read_loop_table(widget._loop_table))
    stored = dict(zip(frame.Parameter, frame.Values))
    assert stored['Exposure Time (ms)'] == '50, 200'
    assert stored['Andor Exposure Time (ms)'] == '1000'
    assert set(stored) == set(paired_definition().Parameter)
    # Same plan as the saved recipe: only the row order of the records differs.
    expected = panel._canonical_loop_order(panel._normalize_loop(paired_definition()))
    assert frame.reset_index(drop=True).equals(expected)
    sequence, _, _ = panel._build_plan(frame, panel._normalize_batch(panel._DEFAULT_BATCH), 'Customized')
    assert [(ctx['Exposure Time (ms)'], ctx['Andor Exposure Time (ms)'], ctx['Stage Position'])
            for ctx in sequence] == [(50, 1000, 0), (200, 1000, 1)]


def test_single_detector_setup_hides_the_andor_column(widget):
    table = widget._loop_table
    table.item(0, 2).setText('PIXIS')
    assert table.isColumnHidden(ANDOR)
    assert table.horizontalHeaderItem(2).text() == 'Values'
    frame = panel._read_loop_table(table)
    assert not any(str(name).startswith('Andor ') for name in frame.Parameter)


def test_missing_andor_value_is_reported_on_its_cell(widget):
    table = widget._loop_table
    table.item(2, ANDOR).setText('')
    assert any('Loop row 3: enter the Andor value' in issue for issue in widget._draft_validation_issues())


def test_new_row_is_added_below_the_selected_row(widget):
    table = widget._loop_table
    before = parameters(table)
    table.selectRow(1)
    widget._loop_add_btn.click()
    assert table.rowCount() == len(before) + 1
    assert parameters(table)[:2] == before[:2] and parameters(table)[3:] == before[2:]
    assert table.item(2, 2).text() == ''
    table.clearSelection()
    widget._loop_add_btn.click()
    assert table.item(table.rowCount() - 1, 2).text() == ''
    assert parameters(table)[:2] == before[:2]
