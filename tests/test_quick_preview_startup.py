"""Keep preview examples useful without 65 durable transactions at startup."""
import json

from app.experiment_metadata import ExperimentHistory
from app.history_reader import read_settings
from ui.quick_preview import PREVIEW_RUN_COUNT, ensure_preview_history


def test_first_fixture_build_uses_one_transaction_and_preserves_history_contract(tmp_path, monkeypatch):
    statements, connections = [], []
    original = ExperimentHistory._connect
    def connect(history):
        connection = original(history)
        connection.set_trace_callback(statements.append)
        connections.append(connection)
        return connection
    monkeypatch.setattr(ExperimentHistory, "_connect", connect)
    history = ensure_preview_history(tmp_path)
    assert len(connections) == 1
    assert sum(statement.startswith("BEGIN") for statement in statements) == 1
    assert statements.count("COMMIT") == 1
    page = history.query_page("PREVIEW", "dual_gate_sweep", page_size=50)
    assert page["total"] == PREVIEW_RUN_COUNT == 65
    assert len(page["rows"]) == 50
    assert page["rows"][0]["experiment_id"] == "preview-run-064"
    last = history.query_page("PREVIEW", "dual_gate_sweep", page=1, page_size=50)
    assert len(last["rows"]) == 15
    path = tmp_path / "preview-run-000.json"
    assert read_settings(path) == {"center_nm": 800, "exposure_ms": 250}
    assert json.loads(path.read_text())["device_id"] == "PREVIEW"
    assert history.query_page("different-sample", "dual_gate_sweep")["total"] == 0


def test_restart_reuses_complete_fixture_without_overwriting_sidecars(tmp_path, monkeypatch):
    ensure_preview_history(tmp_path)
    path = tmp_path / "preview-run-000.json"
    metadata = json.loads(path.read_text())
    metadata["settings"]["loadable"]["exposure_ms"] = 321
    path.write_text(json.dumps(metadata))
    before = path.read_bytes()
    def no_write(*args, **kwargs):
        raise AssertionError("An intact preview fixture should not be rewritten during F5")
    monkeypatch.setattr(type(path), "write_text", no_write)
    history = ensure_preview_history(tmp_path)
    assert path.read_bytes() == before
    assert history.query_page("PREVIEW", "dual_gate_sweep")["total"] == 65


def test_missing_sidecar_and_incomplete_index_are_repaired(tmp_path):
    history = ensure_preview_history(tmp_path)
    missing = tmp_path / "preview-run-007.json"
    missing.unlink()
    ensure_preview_history(tmp_path)
    assert missing.is_file()
    connection = history._connect()
    try:
        with connection:
            connection.execute("DELETE FROM experiments WHERE experiment_id='preview-run-020'")
    finally:
        connection.close()
    repaired = ensure_preview_history(tmp_path)
    assert repaired.query_page("PREVIEW", "dual_gate_sweep")["total"] == 65
