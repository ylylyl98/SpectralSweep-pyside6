from __future__ import annotations

import unittest

from utils.filename_builder import (
    FilenameContext,
    build_base_filename,
    format_laser_power_token,
    resolve_power_uw,
)


def _context(**overrides) -> FilenameContext:
    values = {
        "device_id": "Sample1",
        "point": "p1",
        "tag": "",
        "temperature": "6",
        "mode": "PL",
        "laser_nm": "532",
        "nominal_power_uw": "1100",
        "center_nm": "860",
        "exposure_ms": "2000",
        "accumulations": "1",
        "power_coefficient": 1.0,
    }
    values.update(overrides)
    return FilenameContext(**values)


class FilenamePowerTests(unittest.TestCase):
    def test_manual_power_is_multiplied_by_coefficient(self):
        ctx = _context(power_coefficient=2)

        self.assertEqual(resolve_power_uw(ctx), (2200.0, "nominal"))
        self.assertEqual(format_laser_power_token(ctx), "532nm2200.000uW")
        self.assertEqual(
            build_base_filename(ctx, ["laser_power"]),
            "Sample1_p1_532nm2200.000uW",
        )

    def test_measured_power_is_corrected_exactly_once(self):
        ctx = _context(
            nominal_power_uw="1100",
            measure_power=True,
            measured_power_uw=12.5,
            power_coefficient=2,
        )

        self.assertEqual(resolve_power_uw(ctx), (25.0, "measured"))
        self.assertEqual(format_laser_power_token(ctx), "532nm25.000uW")

    def test_coefficient_one_preserves_manual_power(self):
        self.assertEqual(resolve_power_uw(_context()), (1100.0, "nominal"))

    def test_decimal_coefficient_is_supported(self):
        ctx = _context(nominal_power_uw="10", power_coefficient=0.25)
        self.assertEqual(resolve_power_uw(ctx), (2.5, "nominal"))

    def test_invalid_coefficient_falls_back_to_one(self):
        ctx = _context(power_coefficient="invalid")
        self.assertEqual(resolve_power_uw(ctx), (1100.0, "nominal"))

    def test_missing_power_remains_missing(self):
        ctx = _context(nominal_power_uw="", power_coefficient=2)
        self.assertEqual(resolve_power_uw(ctx), (None, "missing"))

    def test_dot_decimal_style_is_opt_in_and_legacy_p_style_remains_default(self):
        legacy = _context(center_nm=750.25, exposure_ms=125.0)
        dotted = _context(
            center_nm=750.25,
            exposure_ms=125.0,
            decimal_style="dot",
        )

        self.assertIn(
            "750p25nmc_0p125sx1",
            build_base_filename(legacy, ["center", "exposure"]),
        )
        self.assertIn(
            "750.25nmc_0.125sx1",
            build_base_filename(dotted, ["center", "exposure"]),
        )


if __name__ == "__main__":
    unittest.main()
