"""Sweep metadata scales with actual changes without changing legacy exports."""
import json
import os
import threading
from datetime import datetime
from types import SimpleNamespace
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import numpy as np
import pytest
from PySide6.QtWidgets import QApplication

from app import experiment_metadata as metadata_module
from app.experiment_metadata import ExperimentMetadataService, _jsonable
from tests.test_megasweep_sequence import _FakeLF6Controller, _FakeSMUController, _params, _point
from ui.megasweep_panel import (
    CoordSystem, MegaSweepPanel, _MegaSweepWorker,
    _build_csv_metadata_text, _metadata_projection_params,
)
from ui.power_sweep_panel import PowerSweepPanel


@pytest.fixture(scope="module", autouse=True)
def app():
    yield QApplication.instance() or QApplication([])


@pytest.mark.parametrize("panel_type", [PowerSweepPanel, MegaSweepPanel])
@pytest.mark.parametrize("outcome", ["completed", "cancelled", "failed"])
def test_many_output_files_share_one_terminal_metadata_write(tmp_path, panel_type, outcome):
    service = ExperimentMetadataService(tmp_path, tmp_path / "history.sqlite")
    run = service.begin("motion_sweep" if panel_type is PowerSweepPanel else "gate_map_2d", "sample")
    existing = set(tmp_path.iterdir())
    paths = [tmp_path / f"scan-{index}.csv" for index in range(12)]
    paths += [tmp_path / "scan.meta.txt", tmp_path / "scan_manifest.json"]
    for path in paths:
        path.write_text("saved data\n", encoding="utf-8")
    run.register_file(paths[0], "raw", details={"condition_id": "condition-001", "partial": True})
    panel = panel_type()
    panel._experiment_run = run
    panel._run_files_before = existing
    panel._run_failed = outcome == "failed"
    panel._run_finalized = False
    stop = threading.Event()
    if outcome == "cancelled":
        stop.set()
    panel._worker = SimpleNamespace(_stop=stop)
    panel._run_btn.setEnabled(False)
    atomic_write = metadata_module._atomic_json

    def persist(path, value):
        assert not panel._run_btn.isEnabled()
        assert panel._status_lbl.text() not in {"Ready", "Done"}
        return atomic_write(path, value)

    try:
        with patch.object(metadata_module, "_atomic_json", side_effect=persist) as writes, \
                patch.object(service.history, "upsert", wraps=service.history.upsert) as indexes:
            panel._on_finished()
        assert writes.call_count == indexes.call_count == 1
        saved = service.load_metadata(run.path)
        assert saved["status"] == outcome
        if outcome == "failed":
            assert panel._status_lbl.text() == "Error"
        files = {entry["path"]: entry for entry in saved["files"]}
        assert all(path.name in files for path in paths)
        assert files[paths[0].name]["condition_id"] == "condition-001"
        assert files[paths[0].name]["partial"] is True
        if panel_type is MegaSweepPanel:
            assert files["scan.meta.txt"]["compatibility_projection"] is True
            assert files["scan.meta.txt"]["derived_from_experiment_id"] == run.experiment_id
    finally:
        panel._worker = None
        panel.close()


@pytest.mark.parametrize("panel_type", [PowerSweepPanel, MegaSweepPanel])
@pytest.mark.parametrize("stopped", [False, True])
def test_degraded_metadata_overrides_success_or_stop_in_ui(tmp_path, panel_type, stopped):
    service = ExperimentMetadataService(tmp_path, tmp_path / "history.sqlite")
    run = service.begin("motion_sweep" if panel_type is PowerSweepPanel else "gate_map_2d", "sample")
    run.mark_metadata_failure(OSError("fake event persistence failure"), event="capture_started")
    panel = panel_type()
    panel._experiment_run = run
    panel._run_files_before = set(tmp_path.iterdir())
    panel._run_failed = False
    panel._run_finalized = False
    stop = threading.Event()
    if stopped:
        stop.set()
    panel._worker = SimpleNamespace(_stop=stop)
    try:
        panel._on_finished()
        saved = service.load_metadata(run.path)
        assert saved["status"] == "failed"
        assert panel._run_failed is True
        assert panel._status_lbl.text() == "Error"
        assert stop.is_set() is stopped
    finally:
        panel._worker = None
        panel.close()


@pytest.mark.parametrize("coordinate", [CoordSystem.RAW, CoordSystem.PHYSICAL])
def test_compact_projection_preserves_legacy_text_and_exact_plan(tmp_path, coordinate):
    params = _params(tmp_path)
    params["coord"] = coordinate
    params["all_points"] = [_point(i / 100, 0.2) for i in range(4000)]
    params["valid_points"] = params["all_points"][:3500]
    original = json.dumps(_jsonable(params), sort_keys=True)
    projected = _metadata_projection_params(params)
    assert projected["all_point_count"] == 4000
    assert projected["valid_point_count"] == 3500
    assert "all_points" not in projected and "valid_points" not in projected
    assert len(json.dumps(_jsonable(projected))) < len(original) / 100
    now = SimpleNamespace(now=lambda: datetime(2026, 10, 25, 12, 30))
    with patch("ui.megasweep_panel.datetime", now):
        expected = _build_csv_metadata_text(params, np.array([719., 720., 721.]), status="Stopped", completed_points=12)
        actual = _build_csv_metadata_text(_jsonable(projected), np.array([719., 720., 721.]), status="Stopped", completed_points=12)
    assert actual == expected
    assert "# Skipped: 500" in actual
    assert json.dumps(_jsonable(params), sort_keys=True) == original


def test_each_map_journals_small_projection_and_can_rebuild_export(tmp_path):
    params = _params(tmp_path)
    service = ExperimentMetadataService(tmp_path, tmp_path / "history.sqlite")
    run = service.begin("gate_map_2d", "sample", settings=params)
    lf6, smu = _FakeLF6Controller(), _FakeSMUController()
    worker = _MegaSweepWorker(params, smu, lf6, run)
    with patch("ui.megasweep_panel._get_wavelengths", side_effect=lambda _spec, _lf6, center, tol_nm: np.array([center - 1, center, center + 1])):
        worker._run_sweep(params)
    records = run.read_events(limit=100)
    initial = [event for event in records if event["event"] == "compatibility_state" and "params" in event.get("changes", {})]
    assert len(initial) == 2
    assert all("all_points" not in event["changes"]["params"] for event in initial)
    assert lf6.adapter.acquire_count == 4
    for path in tmp_path.glob("*.meta.txt"):
        state = run.reconstruct_compatibility_state(path.name)
        assert state["status"] == "Complete" and state["completed_points"] == 2
        assert state["params"]["all_point_count"] == state["params"]["valid_point_count"] == 2
        assert "# CoordinateSystem: Raw" in path.read_text()
        assert "# CompletedPoints: 2" in path.read_text()
    # The scientific plan is retained once in the complete experiment record.
    saved = service.load_metadata(run.path)
    assert saved["settings"]["requested"]["all_points"] == _jsonable(params["all_points"])
    assert saved["settings"]["requested"]["valid_points"] == _jsonable(params["valid_points"])


def test_failed_map_retains_durable_projection_and_saved_rows(tmp_path):
    params = _params(tmp_path)
    service = ExperimentMetadataService(tmp_path, tmp_path / "history.sqlite")
    run = service.begin("gate_map_2d", "sample", settings=params)
    lf6, smu = _FakeLF6Controller(), _FakeSMUController()
    worker = _MegaSweepWorker(params, smu, lf6, run)
    capture = lf6.adapter.acquire
    calls = 0

    def fail_second_capture():
        nonlocal calls
        calls += 1
        if calls == 2:
            raise RuntimeError("fake acquisition failure")
        return capture()

    with patch.object(lf6.adapter, "acquire", side_effect=fail_second_capture), \
            patch("ui.megasweep_panel._get_wavelengths", side_effect=lambda _spec, _lf6, center, tol_nm: np.array([center - 1, center, center + 1])):
        with pytest.raises(RuntimeError, match="fake acquisition failure"):
            worker._run_sweep(params)
    meta = next(tmp_path.glob("*.meta.txt"))
    state = run.reconstruct_compatibility_state(meta.name)
    assert state["status"] == "Failed" and state["completed_points"] == 1
    assert "all_points" not in state["params"]
    assert len(next(tmp_path.glob("*.csv")).read_text().splitlines()) == 2
    assert "# Status: Failed" in meta.read_text()
    assert "# CompletedPoints: 1" in meta.read_text()
