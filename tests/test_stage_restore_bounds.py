import importlib.util
import sys
import unittest
from pathlib import Path
from types import ModuleType
from unittest.mock import patch


# Load the adapter without importing or connecting the optional hardware SDK.
sdk = ModuleType("pylablib.devices")
sdk.Thorlabs = object()
spec = importlib.util.spec_from_file_location(
    "stage_restore_adapter", Path(__file__).resolve().parents[1] / "app/devices/stage_elliptec_adapter.py"
)
adapter_module = importlib.util.module_from_spec(spec)
with patch.dict(sys.modules, {"pylablib.devices": sdk}):
    spec.loader.exec_module(adapter_module)


class StageRestoreBoundsTests(unittest.TestCase):
    def setUp(self):
        self.adapter = adapter_module.ElliptecLinearStage.__new__(adapter_module.ElliptecLinearStage)

    def test_small_boundary_excursions_restore_to_endpoint(self):
        for observed, target in ((-1, 0), (-0.001, 0), (3600.001, 3600), (3601, 3600), (12.3, 12.3)):
            with self.subTest(observed=observed):
                self.assertEqual(self.adapter.normalize_restore_position(observed), target)

    def test_larger_finite_excursions_restore_to_boundary(self):
        for observed, target in ((-1.001, 0), (3601.001, 3600), (-10, 0), (3700, 3600)):
            with self.subTest(observed=observed):
                self.assertEqual(self.adapter.normalize_restore_position(observed), target)

    def test_nonfinite_readbacks_are_rejected(self):
        for observed in (float("nan"), float("inf"), -float("inf")):
            with self.subTest(observed=observed), self.assertRaises(ValueError):
                self.adapter.normalize_restore_position(observed)

    def test_regular_moves_keep_strict_limits(self):
        for target in (-0.001, 3600.001):
            with self.subTest(target=target), self.assertRaisesRegex(ValueError, "requested"):
                self.adapter.validate_position(target)


if __name__ == "__main__":
    unittest.main()
