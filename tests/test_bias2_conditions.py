import pytest

from utils.mcd_common import resolve_condition_line, validate_gate_conditions
from app.engine.mcd2100_worker import MCD2100Worker


def test_gate_condition_defaults_and_follow_target():
    old = resolve_condition_line({"mode": "direct", "vtg_v": 1, "vbg_v": 2}, 1)
    assert old["bias2_mode"] == "off"
    row = resolve_condition_line({"mode": "direct", "vtg_v": 0, "vbg_v": 0,
                                  "vbias_v": 1, "bias2_mode": "follow", "bias2_offset_v": 1}, 1)
    assert row["vbias2_target_v"] == 2
    with pytest.raises(ValueError, match="Vbias2"):
        validate_gate_conditions([row], 1.5)


def test_worker_preserves_second_bias_per_condition():
    rows = MCD2100Worker._normalize_conditions([
        {"vtg_v": 0, "vbg_v": 0, "vbias_v": 0, "bias2_mode": "independent", "vbias2_v": 0},
        {"vtg_v": 0, "vbg_v": 0, "vbias_v": 2, "bias2_mode": "follow", "bias2_offset_v": 1},
    ])
    assert rows[0]["bias2_mode"] == "independent"
    assert rows[0]["vbias2_target_v"] == 0
    assert rows[1]["vbias2_target_v"] == 3



def test_independent_bias2_list_expands_each_gate_condition():
    from utils.mcd_common import expand_bias2_conditions
    rows = expand_bias2_conditions([{"vtg_v": 0, "vbg_v": 0}, {"vtg_v": 1, "vbg_v": 0}],
                                   {"bias2_mode": "independent", "vbias2_v": 9}, "0,1", 1)
    assert [(r["vtg_v"], r["vbias2_target_v"]) for r in rows] == [(0, 0), (0, 1), (1, 0), (1, 1)]


def test_continuous_worker_checks_bias2_before_output_or_instrument_work():
    worker = object.__new__(MCD2100Worker)
    worker.apply_voltages = True
    worker.conditions = [{"bias2_mode": "follow", "vbias_v": 0, "bias2_offset_v": 0}]
    class Optical:
        def preflight_bias2(self, conditions):
            assert conditions == worker.conditions
            raise RuntimeError("second bias unavailable")
    worker.optical = Optical()
    worker._paths = lambda: pytest.fail("output paths must not be created before preflight")
    with pytest.raises(RuntimeError, match="second bias unavailable"):
        worker.run()
