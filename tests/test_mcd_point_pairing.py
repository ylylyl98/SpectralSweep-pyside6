import math

import pytest

from app.engine.mcd_point_pairing import FieldStability, field_points, validate_pair_fields


def test_grid_preserves_endpoints_and_direction():
    assert field_points(-0.1, 0.12, 0.05) == [-0.1, -0.05, 0, 0.05, 0.1, 0.12]
    assert field_points(0.12, -0.1, 0.05) == [0.12, 0.07, 0.02, -0.03, -0.08, -0.1]
    assert field_points(0, 0.3, 0.1) == [0, 0.1, 0.2, 0.3]
    assert field_points(0.2, 0.2, 0.05) == [0.2]


@pytest.mark.parametrize("step", [0, -0.1, math.nan, math.inf, 1e-9])
def test_invalid_grid_is_rejected(step):
    with pytest.raises(ValueError):
        field_points(0, 1, step)


def test_stability_restarts_after_excursion_target_change_or_bad_data():
    stable = FieldStability(0.01, 1)
    assert not stable.observe(0, 0, 0)
    assert not stable.observe(0.01, 0, 0.9)
    assert not stable.observe(0, 0, 1)
    assert stable.observe(0, 0, 2)
    assert not stable.observe(0.005, 0.005, 2.1)
    assert not stable.observe(math.nan, 0.005, 3)
    assert not stable.observe(0.005, 0.005, 4)
    assert stable.observe(0.005, 0.005, 5)
    assert not stable.observe(0.005, 0.005, 1)


def test_pair_checks_both_target_error_and_pair_mismatch():
    assert validate_pair_fields([0.001, 0.002, 0.003, 0.004], 0)
    assert not validate_pair_fields([-0.009, 0.009], 0)
    assert not validate_pair_fields([0.01, 0], 0)
    assert not validate_pair_fields([math.nan, 0], 0)
    assert not validate_pair_fields([], 0)


def test_zero_hold_still_requires_good_field():
    stable = FieldStability(0.01, 0)
    assert not stable.observe(0.02, 0, 0)
    assert stable.observe(0, 0, 1)
