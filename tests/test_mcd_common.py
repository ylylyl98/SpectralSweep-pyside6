import unittest

from utils.mcd_common import (
    FilenameContext, MODE_DIRECT, MODE_DOPING_EFIELD, build_condition_batch,
    build_mcd2100_filename, expand_condition_inputs, gate_ratio_from_factors,
    parse_numeric_spec,
)


class MCDCommonFilenameTests(unittest.TestCase):
    def test_canonical_forward_backward_names_encode_actual_endpoints(self):
        forward = build_mcd2100_filename("D237", 1, -2, 2, "forward")
        backward = build_mcd2100_filename("D237", 1, 2, -2, "backward")
        self.assertTrue(forward.startswith("D237_MCD_G01_B-2to+2T_"))
        self.assertTrue(forward.endswith("_forward.csv"))
        self.assertTrue(backward.startswith("D237_MCD_G01_B+2to-2T_"))
        self.assertTrue(backward.endswith("_backward.csv"))

    def test_unsafe_device_is_sanitized(self):
        name = build_mcd2100_filename("D:/237 bad?device", 2, -1, 1, "forward")
        self.assertNotIn(":", name)
        self.assertNotIn("/", name)
        self.assertNotIn("?", name)
        self.assertIn("D237-baddevice_MCD_G02", name)

    def test_two_sided_gate_ratio_accepts_factor_on_either_side(self):
        self.assertEqual(gate_ratio_from_factors(1, 5), 5)
        self.assertEqual(gate_ratio_from_factors(2, 1), .5)
        with self.assertRaisesRegex(ValueError, "non-zero"):
            gate_ratio_from_factors(0, 1)

    def test_numeric_lists_ranges_and_paired_singleton_broadcast(self):
        self.assertEqual(parse_numeric_spec("-1:1:1, 3", "values"), [-1, 0, 1, 3])
        self.assertEqual(
            expand_condition_inputs([1], [4, 5, 6], "paired"),
            [(1, 4), (1, 5), (1, 6)],
        )
        with self.assertRaisesRegex(ValueError, "equal lengths"):
            expand_condition_inputs([1, 2], [3, 4, 5], "paired")

    def test_direct_and_coordinate_batches_support_paired_and_grid(self):
        direct = build_condition_batch(
            MODE_DIRECT, "1,2", "3,4", "paired", 2, voltage_limit=10,
        )
        self.assertEqual([(row["vtg_v"], row["vbg_v"]) for row in direct], [(1, 3), (2, 4)])
        coordinates = build_condition_batch(
            MODE_DOPING_EFIELD, "2,4", "0,2", "grid", 2, voltage_limit=10,
        )
        self.assertEqual(len(coordinates), 4)
        self.assertEqual(
            (coordinates[0]["vtg_v"], coordinates[0]["vbg_v"]),
            (1, .5),
        )

    def test_batch_compliance_failure_rejects_the_complete_batch(self):
        with self.assertRaisesRegex(ValueError, "compliance"):
            build_condition_batch(
                MODE_DIRECT, "1,20", "0", "paired", 1, voltage_limit=10,
            )


if __name__ == "__main__":
    unittest.main()
