"""Exercise restart and sample-selection paths with real CSVs, without hardware."""
import copy
import json
from pathlib import Path
from unittest.mock import Mock, patch

import numpy as np
import pytest
from PySide6.QtWidgets import QApplication, QMainWindow, QStatusBar, QTabWidget

from app.sample_settings import SampleSettingsStore
from app.spectrum_reference_state import sample_spectrum_folder
from tests.test_spectrum_references import _Controller
from ui.main_window import MainWindow, _SharedSampleIdBinder
from ui.spectrum_panel import SpectrumPanel
from utils.config import AppConfig, cfg


@pytest.fixture
def workspace(monkeypatch, tmp_path):
    app = QApplication.instance() or QApplication([])
    monkeypatch.setattr(cfg, "session", AppConfig().session)
    monkeypatch.setattr(cfg, "filename", copy.deepcopy(cfg.filename))
    monkeypatch.setattr(cfg, "lf6", copy.deepcopy(cfg.lf6))
    cfg.session.sample_id = "sample A"
    cfg.filename.base_out = str(tmp_path)
    save = cfg.save
    monkeypatch.setattr(cfg, "save", lambda: save(tmp_path / "config.json"))
    widgets = []

    def panel():
        controller = _Controller()
        controller.identity.update(backend="lightfield", axis_unit="nm")
        widget = SpectrumPanel(controller)
        widgets.append(widget)
        return widget

    def window():
        host = MainWindow.__new__(MainWindow)
        QMainWindow.__init__(host)
        widgets.append(host)
        host._spectrum = panel()
        host._status = QStatusBar(host)
        host._sample_id_binder = _SharedSampleIdBinder([], cfg.session.sample_id, host)
        host._sample_store = SampleSettingsStore({"sample_profiles": cfg.session.sample_profiles})
        host._session_panels = {"spectrum": host._spectrum}
        host._tabs = QTabWidget(host)
        host._tabs.addTab(host._spectrum, "Spectrum")
        host._tab_ids = {host._spectrum: "spectrum"}
        host._refresh_sample_selector = Mock()
        host._refresh_history = Mock()
        host._restore_session()
        return host

    yield panel, window
    # No real MainWindow.closeEvent: it owns unrelated hardware services.
    for widget in widgets:
        widget.hide()
    for widget in widgets:
        if isinstance(widget, SpectrumPanel):
            widget.unbind_controller_signals()
        widget.deleteLater()
    app.processEvents()


def add(panel, name="saved spectrum", *, sample=None, auto_save=True):
    return panel.add_reference(
        [700., 701.], [4., 9.], name=name, auto_save=auto_save,
        settings_snapshot={"sample_id": sample or cfg.session.sample_id,
                           "instrument_identity": {"backend": "lightfield", "axis_unit": "nm"}},
    )


def round_trip(panel):
    return json.loads(json.dumps(panel.capture_session_state()))


def test_add_button_reopens_from_saved_config_with_names_colors_and_visibility(workspace, tmp_path):
    _, window = workspace
    host = window()
    panel = host._spectrum
    ctrl = panel._ctrl
    ctrl.connected.emit([])
    panel._add_spectrum_btn.click()
    ctrl.settings_applied.emit()
    ctrl.spectrum_ready.emit(np.array([700., 701.]), np.array([4., 9.]))
    ref = panel.references[0]
    assert ref["saved"]
    panel.set_reference_color(ref["id"], "#ab1234")
    ref["_row_name"].setText("edited before closing")
    ref["_row_checkbox"].setChecked(False)
    host._persist_session()  # The same synchronous call used on app shutdown.
    library = cfg.session.panels["spectrum"]["reference_library"]
    assert "recovery" not in library["entries"][0]
    restored_config = AppConfig()
    restored_config.load(tmp_path / "config.json")
    cfg.session = restored_config.session
    reopened = window()._spectrum
    restored = reopened.references[0]
    assert restored["name"] == "edited before closing"
    assert restored["color"] == "#ab1234"
    assert not restored["visible"]
    assert not reopened._reference_list.item(0).isHidden()
    assert restored["setup_key"] == "lightfield"
    np.testing.assert_array_equal(restored["counts"], [4., 9.])
    assert reopened._last_data is None
    assert reopened._ctrl.apply_calls == [] and reopened._ctrl.acquire_calls == 0
    reopened._set_reference_visible(restored["id"], True)
    assert reopened._spec_plot._reference_curves[restored["id"]].isVisible()


def test_switch_isolates_new_samples_and_restores_both_lists(workspace):
    _, window = workspace
    host = window()
    panel = host._spectrum
    add(panel, "A")
    panel.push_spectrum([700, 701], [3, 4])
    panel._exposure.setValue(123)
    assert host._commit_sample_selection("sample B")
    assert panel.references == [] and panel._last_data is None
    assert panel._exposure.value() == 123  # New sample still inherits controls.
    add(panel, "B")
    assert host._commit_sample_selection("sample B")  # Same ID must not clear it.
    assert len(panel.references) == 1
    assert host._commit_sample_selection("sample A")
    assert [r["name"] for r in panel.references] == ["A"]
    assert host._commit_sample_selection("sample B")
    assert [r["name"] for r in panel.references] == ["B"]
    host._persist_session()
    assert [r["name"] for r in window()._spectrum.references] == ["B"]


def test_legacy_recovery_matches_metadata_and_removal_stays_removed(workspace, tmp_path):
    make_panel, _ = workspace
    cfg.session.sample_id = "A/B"
    old = make_panel()
    ref = add(old, "legacy A", sample="A/B")
    add(old, "different sample with same folder", sample="A:B")
    old.push_spectrum([700, 701], [3, 4])
    old.save_current(Path(ref["path"]).parent / "manual.csv")
    reopened = make_panel()
    reopened.restore_sample_references("A/B", {})
    assert [r["name"] for r in reopened.references] == ["legacy A"]
    reopened.remove_reference(reopened.references[0]["id"])
    state = round_trip(reopened)
    reopened.restore_sample_references("A/B", state)
    assert reopened.references == []
    assert Path(ref["path"]).exists()  # Removing from the list never deletes data.


def test_unavailable_file_does_not_block_other_references_or_lose_its_record(workspace):
    make_panel, _ = workspace
    panel = make_panel()
    missing = add(panel, "missing")
    add(panel, "available")
    state = round_trip(panel)
    path = Path(missing["path"])
    contents = path.read_bytes()
    path.unlink()
    reopened = make_panel()
    reopened.restore_sample_references("sample A", state)
    assert [r["name"] for r in reopened.references] == ["available"]
    assert "could not be restored" in reopened._status_lbl.text()
    retained = round_trip(reopened)
    assert len(retained["reference_library"]["entries"]) == 2
    path.write_bytes(contents)
    reopened.restore_sample_references("sample A", retained)
    assert [r["name"] for r in reopened.references] == ["missing", "available"]
    reopened.restore_sample_references("sample A", retained)
    assert len(reopened.references) == 2


def test_failed_save_remains_retryable_after_restart_with_frozen_sample(workspace):
    make_panel, _ = workspace
    panel = make_panel()
    with patch.object(panel, "_save_spectrum_data", side_effect=OSError("disk full")):
        failed = add(panel)
    assert not failed["saved"]
    state = round_trip(panel)
    cfg.session.sample_id = "another sample"
    reopened = make_panel()
    reopened.restore_sample_references("sample A", state)
    ref = reopened.references[0]
    assert not ref["saved"]
    np.testing.assert_array_equal(ref["counts"], [4., 9.])
    assert reopened.retry_reference_save(ref["id"])
    metadata = json.loads(Path(ref["metadata_path"]).read_text(encoding="utf-8"))
    assert metadata["device"]["sample_id"] == "sample A"
    assert "recovery" not in round_trip(reopened)["reference_library"]["entries"][0]


def test_duplicated_profile_does_not_copy_source_sample_spectra(workspace):
    make_panel, _ = workspace
    panel = make_panel()
    add(panel)
    source = round_trip(panel)
    panel.restore_sample_references("new sample", source)
    assert panel.references == []


def test_imported_pixel_reference_and_raw_counts_survive_normalized_restart(workspace, tmp_path):
    make_panel, _ = workspace
    panel = make_panel()
    path = tmp_path / "imported.csv"
    path.write_text("pixel,intensity_counts\n0,3\n1,6\n", encoding="utf-8")
    panel.load_spectra([path], normalize=True)
    state = round_trip(panel)
    reopened = make_panel()
    reopened.restore_session_state(state)
    reopened.restore_sample_references("sample A", state)
    ref = reopened.references[0]
    assert ref["axis_unit"] == "pixel"
    assert reopened._spec_plot.axis_unit == "pixel"
    np.testing.assert_array_equal(ref["counts"], [3., 6.])
    np.testing.assert_array_equal(reopened._spec_plot._reference_curves[ref["id"]].yData, [.5, 1.])
    assert path.read_text(encoding="utf-8") == "pixel,intensity_counts\n0,3\n1,6\n"


def test_load_dialog_opens_selected_sample_spectrum_folder(workspace, tmp_path):
    _, window = workspace
    host = window()
    a = sample_spectrum_folder(tmp_path, "sample A")
    b = sample_spectrum_folder(tmp_path, "B/C")
    a.mkdir(parents=True)
    b.mkdir(parents=True)
    with patch("ui.spectrum_panel.QFileDialog.getOpenFileNames", return_value=([], "")) as dialog:
        host._spectrum._load_spectra_dialog()
        assert Path(dialog.call_args.args[2]) == a
        host._commit_sample_selection("B/C")
        host._spectrum._load_spectra_dialog()
        assert Path(dialog.call_args.args[2]) == b
        host._commit_sample_selection("brand new sample")
        host._spectrum._load_spectra_dialog()
        assert Path(dialog.call_args.args[2]) == tmp_path
