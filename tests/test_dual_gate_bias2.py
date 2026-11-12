import pandas as pd
import pytest

from ui.presets_panel import _normalize_batch, _resolve_sweep_vectors, _required_smu_roles


def row(**values):
    return {"Run": True, "frames": 3, "Vbg_start": 0, "Vbg_stop": 0,
            "Vtg_start": 0, "Vtg_stop": 0, "Vbias_start": 1, "Vbias_stop": 2, **values}


def test_old_rows_leave_second_bias_off():
    old = _normalize_batch(pd.DataFrame([row()])).iloc[0]
    assert old["bias2_mode"] == "off"
    assert _resolve_sweep_vectors(old)["vbias2_points"] is None


def test_independent_and_follow_sweep():
    independent = row(bias2_mode="independent", Vbias2_start=-1, Vbias2_stop=1)
    assert _resolve_sweep_vectors(independent)["vbias2_points"] == [-1, 0, 1]
    follow = row(bias2_mode="follow", bias2_offset_v=0.25)
    assert _resolve_sweep_vectors(follow)["vbias2_points"] == [1.25, 1.75, 2.25]
    schedule = [{"row": follow}]
    assert _required_smu_roles([], pd.DataFrame(), schedule) == ("Vbg", "Vtg", "Vbias", "Vbias2")


def test_follow_requires_first_bias_and_limits_resolved_target():
    with pytest.raises(ValueError, match="requires Vbias"):
        _resolve_sweep_vectors(row(Vbias_start="", Vbias_stop="", bias2_mode="follow", bias2_offset_v=0))
    with pytest.raises(ValueError, match="within"):
        _resolve_sweep_vectors(row(bias2_mode="follow", bias2_offset_v=200))


def test_quick_mode_choice_and_relevant_columns(monkeypatch):
    from PySide6.QtCore import Qt
    from tests.quick_helpers import quick_application
    from ui.presets_panel import PresetsPanel, BATCH_SCHEMA, _configure_batch_table_columns
    from ui.quick_table_editor import TableEditorModel
    from utils.config import cfg

    quick_application()
    monkeypatch.setattr(cfg.smu, "vbias2_resource", "fake")
    monkeypatch.setattr(cfg, "save", lambda: None)
    panel = PresetsPanel()
    table = panel._batch_table
    _configure_batch_table_columns(table)
    model = TableEditorModel(table)
    mode_col = BATCH_SCHEMA.index("bias2_mode")
    visible_mode = model._columns.index(mode_col)
    assert model.cellAt(0, visible_mode)["options"] == ["off", "independent", "follow"]
    assert table.isColumnHidden(BATCH_SCHEMA.index("Vbias2_start"))
    assert model.editCell(0, visible_mode, 2)
    model.refresh()
    assert table.item(0, mode_col).text() == "follow"
    assert table.isColumnHidden(BATCH_SCHEMA.index("Vbias2_start"))
    assert not table.isColumnHidden(BATCH_SCHEMA.index("bias2_offset_v"))
    assert not table.item(0, BATCH_SCHEMA.index("Vbias2_start")).flags() & Qt.ItemFlag.ItemIsEditable
    assert model.editCell(0, model._columns.index(mode_col), 1)
    model.refresh()
    assert not table.isColumnHidden(BATCH_SCHEMA.index("Vbias2_start"))
    assert table.isColumnHidden(BATCH_SCHEMA.index("bias2_offset_v"))
    assert table.item(0, BATCH_SCHEMA.index("Vbias2_start")).flags() & Qt.ItemFlag.ItemIsEditable
    model.shutdown()
    panel.dispose_reloaded_page()
    panel.close()
