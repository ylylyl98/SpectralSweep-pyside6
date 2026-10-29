"""Add Quick surfaces to the existing hardware-free, isolated preview."""
from __future__ import annotations

import json
from pathlib import Path

from PySide6.QtWidgets import QTabWidget, QVBoxLayout, QWidget
from app.experiment_metadata import ExperimentHistory
from ui.quick_controls import install_spectrum_controls
from ui.quick_history import QuickHistoryWidget


PREVIEW_RUN_COUNT = 65
_FIXTURE_VERSION = 1


def ensure_preview_history(root: Path):
    """Build the disposable example index once, with one SQLite transaction.

    This is fixture construction for the isolated preview only. Production
    history keeps using ExperimentHistory.upsert and its durability policy.
    """
    root = Path(root)
    history = ExperimentHistory(root / "quick-preview-history.sqlite")
    marker = root / "quick-preview-fixture.json"
    expected = {"version": _FIXTURE_VERSION, "run_count": PREVIEW_RUN_COUNT}
    try:
        if (json.loads(marker.read_text(encoding="utf-8")) == expected
                and all((root / f"preview-run-{index:03d}.json").is_file() for index in range(PREVIEW_RUN_COUNT))
                and history.query_page("PREVIEW", "dual_gate_sweep", page_size=1)["total"] == PREVIEW_RUN_COUNT):
            return history
    except (OSError, ValueError):
        pass
    rows = []
    for index in range(PREVIEW_RUN_COUNT):
        path = root / f"preview-run-{index:03d}.json"
        metadata = {
            "experiment_id": f"preview-run-{index:03d}", "device_id": "PREVIEW",
            "experiment_type": "dual_gate_sweep", "status": "completed" if index % 7 else "failed",
            "started_utc": f"2026-10-26T10:{index // 60:02d}:{index % 60:02d}Z",
            "metadata_path": str(path),
            "settings": {"loadable": {"center_nm": 800, "exposure_ms": 250}},
        }
        path.write_text(json.dumps(metadata), encoding="utf-8")
        rows.append((metadata["experiment_id"], metadata["device_id"], metadata["experiment_type"],
                     metadata["started_utc"], None, metadata["status"], str(path.resolve()),
                     json.dumps(metadata["settings"], sort_keys=True), "{}"))
    connection = history._connect()
    try:
        with connection:
            connection.executemany(
                "INSERT OR REPLACE INTO experiments "
                "(experiment_id, device_id, experiment_type, started_utc, completed_utc, "
                "status, metadata_path, settings_json, summary_json) VALUES (?,?,?,?,?,?,?,?,?)", rows)
    finally:
        connection.close()
    # Mark complete after the sidecars and index are committed. A partial first
    # build is repaired on the next launch; a normal F5 keeps the same records.
    marker.write_text(json.dumps(expected), encoding="utf-8")
    return history


def enhance_preview(window, root: Path):
    window.setWindowTitle("SpectralSweep — Qt Quick PREVIEW (simulated)")
    window.quick_commands = install_spectrum_controls(window.panel)
    history = ensure_preview_history(root)
    window.quick_history = QuickHistoryWidget(history, root, "PREVIEW", parent=window)
    tabs = QTabWidget()
    window.preview_tabs = tabs
    layout = window.centralWidget().layout()
    layout.removeWidget(window.panel)
    tabs.addTab(window.panel, "Spectrum")
    tabs.addTab(window.quick_history, "Run history")
    # Sequence review is optional in the simulator. Construct it only when
    # selected so adding an example does not delay the initial Spectrum frame.
    sequence_page = QWidget()
    sequence_layout = QVBoxLayout(sequence_page)
    sequence_layout.setContentsMargins(0, 0, 0, 0)
    sequence_index = tabs.addTab(sequence_page, "Dual Gate sequence")
    window.quick_sequence_demo = None
    def show_sequence(index):
        if index == sequence_index and window.quick_sequence_demo is None:
            from ui.dual_gate_sequence_demo import DualGateSequenceDemo
            window.quick_sequence_demo = DualGateSequenceDemo(sequence_page)
            sequence_layout.addWidget(window.quick_sequence_demo)
    tabs.currentChanged.connect(show_sequence)
    layout.addWidget(tabs, 1)
    window.destroyed.connect(window.quick_history.shutdown)
    window._preview_scratch_root = root
