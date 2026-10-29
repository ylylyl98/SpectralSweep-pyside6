"""The profiler's statistics stay correct without importing Qt/device runtime."""
from tools.profile_quick_workspaces import distribution, summarize


def test_heartbeat_distribution_preserves_tail_and_stall_thresholds():
    result = distribution([110, 10, 50, 20])
    assert result == {"count": 4, "p50_ms": 35., "p95_ms": 101., "max_ms": 110,
                      "stalls_over_50ms": 1, "stalls_over_100ms": 1}
    assert distribution([])["p95_ms"] is None
    assert distribution([12])["p95_ms"] == 12


def run(value, fingerprint="same"):
    return {"startup": {key: value for key in (
        "process_entry_to_first_grab_ms", "construction_ms", "qml_synchronous_load_ms")},
        "warm_switch": {"page": [{"through_grab_ms": value}, {"through_grab_ms": value + 2}]},
        "first_switch": {"page": {"through_grab_ms": value}},
        "input": {"complete_string_to_source_ms": value},
        "idle": [{"page": page, "single_core_cpu_percent": value, "rss_bytes": value * 1000}
                 for page in ("spectrum", "mega_sweep")],
        "heartbeat": {"idle_" + page: {"samples_ms": [10, value]} for page in ("spectrum", "mega_sweep")},
        "source_fingerprint": {"sha256": fingerprint},
        "warnings": [], "callback_errors": [], "hardware_import_attempts": [], "closed": True}


def test_repeated_process_summary_uses_medians_and_detects_source_changes():
    runs = [run(1000), run(20), run(10)]
    result = summarize(runs)
    assert result["startup_ms"]["construction_ms"] == 20
    assert result["warm_switch_ms"]["page"] == 21
    assert result["idle"]["spectrum"]["single_core_cpu_percent"] == 20
    assert result["clean"] and result["source_consistent"]
    runs[-1]["source_fingerprint"]["sha256"] = "changed"
    runs[-1]["callback_errors"].append("failed callback")
    assert not summarize(runs)["source_consistent"]
    assert not summarize(runs)["clean"]
