from __future__ import annotations

import unittest

from app.devices.stage_profiles import get_linear_stage_profile


class StageProfileTests(unittest.TestCase):
    def test_static_profiles_match_supported_stage_limits(self):
        elliptec = get_linear_stage_profile("elliptec")
        self.assertEqual(
            (
                elliptec.minimum_position,
                elliptec.maximum_position,
                elliptec.default_address_kind,
                elliptec.default_axis,
            ),
            (0.0, 3600.0, "com", None),
        )

        esp300 = get_linear_stage_profile("esp300")
        self.assertEqual(
            (
                esp300.minimum_position,
                esp300.maximum_position,
                esp300.default_address_kind,
                esp300.default_axis,
            ),
            (0.0, 50.0, "visa", 3),
        )

    def test_unknown_backend_is_rejected(self):
        with self.assertRaises(ValueError):
            get_linear_stage_profile("unknown")


if __name__ == "__main__":
    unittest.main()
