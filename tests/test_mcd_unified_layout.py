"""Both magnet pages expose the same structure without flattening diagnostics."""
from PySide6.QtWidgets import QWidget
from PySide6.QtTest import QTest

from tests.test_mcd_workspace_layout import workspace


def test_common_field_rows_keep_their_ownership(workspace):
    panel, page_id, quick, sections, coverage = workspace
    group = panel._mcd_settings_groups["FieldSweep"]
    for name in ("McdAcquisitionMode", "McdFieldEndpoints", "McdPointSettings", "McdStableDuration"):
        row = group.findChild(QWidget, name)
        assert row is not None
        assert row.property("quickSectionBoundary")
    assert panel._acquisition_mode.itemData(0) == "continuous"
    assert panel._acquisition_mode.itemData(1) == "point_pairing"


def test_disclosures_use_arrows_and_telemetry_keeps_its_grid(workspace):
    panel, page_id, quick, sections, coverage = workspace
    if not quick:
        return
    key = "APS100 — attoDRY1000" if page_id == "mcd" else "attoDRY2100 connection and telemetry"
    sections[key].set_expanded(True)
    panel.show()
    QTest.qWait(100)
    fields = {source: field for form in coverage["forms"] for field, source in form.adapter._fields.items()}
    if page_id == "mcd":
        toggles = (panel._advanced_toggle, panel._metadata_toggle)
        readback = panel._field
        content = panel._transition_widget
    else:
        toggles = (panel._connection_advanced.toggle, panel._temperature_diagnostics.toggle)
        readback = panel.field_value
        content = panel._connection_advanced.content
    for toggle in toggles:
        assert fields[toggle].kind == "disclosure"
    assert readback not in fields  # Labels stay in their two-column native grid.
    assert content.isHidden()
    assert fields[toggles[0]].trigger()
    assert not content.isHidden()
    assert fields[toggles[0]].trigger()
    assert content.isHidden()
