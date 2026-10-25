"""History distinguishes file summaries from unrecorded capture attempts."""
import copy

import pytest

from app.experiment_metadata import ExperimentMetadataService


def file_metadata(status="running"):
    return {
        "status": status,
        "event_log": {"path": "run.experiment.events.jsonl"},
        "recording_policy": {
            "mode": "file", "per_capture_events": False,
            "acquisition_attempt_counts": "not recorded",
        },
        "acquisitions": {"count": 0, "total_count": 0, "outputs": []},
    }


def test_running_file_preview_explains_boundary_progress_without_zero_capture_claim():
    metadata = file_metadata()
    before = copy.deepcopy(metadata)
    text = ExperimentMetadataService.format_history_preview(metadata)
    assert "[Recording policy]" in text
    assert '"mode": "file"' in text
    assert "Capture attempts: not recorded (file mode)" in text
    assert "successful CSV writes" in text
    assert "File summaries are saved at file boundaries" in text
    assert "available in Events" in text
    assert "run is interrupted" in text
    assert "Per-capture records" not in text
    assert "[Acquisitions]" not in text
    assert '"count": 0' not in text
    assert metadata == before


@pytest.mark.parametrize("status", ["completed", "cancelled", "failed"])
def test_terminal_file_preview_shows_completed_points_before_large_plan(status):
    metadata = file_metadata(status)
    metadata["summary"] = {"completed_points": 123, "metadata_recording_policy": "file"}
    metadata["settings"] = {"requested": [{"values": list(range(100))} for _ in range(100)]}
    text = ExperimentMetadataService.format_history_preview(metadata)
    assert "[Summary]" in text
    assert '"completed_points": 123' in text
    assert text.index("[Summary]") < text.index("[Setup]")
    assert "Capture attempts: not recorded" in text
    assert "An active file's progress" not in text


def test_capture_mode_keeps_existing_counts_and_running_guidance():
    metadata = {
        "status": "running", "event_log": {"path": "run.events.jsonl"},
        "acquisitions": {"count": 7},
    }
    text = ExperimentMetadataService.format_history_preview(metadata)
    assert "last saved snapshot" in text
    assert "Per-capture records are available in Events" in text
    assert "[Acquisitions]" in text
    assert '"count": 7' in text
    assert "not recorded (file mode)" not in text
