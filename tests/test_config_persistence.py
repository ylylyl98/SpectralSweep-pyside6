from __future__ import annotations

import json
import tempfile
import unittest
from dataclasses import asdict
from pathlib import Path

from utils.config import AppConfig


class ConfigPersistenceTests(unittest.TestCase):
    def test_rotation_profiles_round_trip_per_slot(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "config.json"
            source = AppConfig()
            source.rotation.rot1.esp300_velocity_fraction = 1.0
            source.rotation.rot1.esp300_acceleration_fraction = 0.5
            source.rotation.rot2.esp300_velocity_fraction = 0.75
            source.rotation.rot2.esp300_acceleration_fraction = 0.25
            source.save(path)

            restored = AppConfig()
            restored.load(path)

            self.assertEqual(restored.rotation.rot1.esp300_velocity_fraction, 1.0)
            self.assertEqual(restored.rotation.rot1.esp300_acceleration_fraction, 0.5)
            self.assertEqual(restored.rotation.rot2.esp300_velocity_fraction, 0.75)
            self.assertEqual(restored.rotation.rot2.esp300_acceleration_fraction, 0.25)

    def test_full_2100_dataclass_asdict_roundtrip(self):
        with tempfile.TemporaryDirectory() as tmp:
            path=Path(tmp)/"config.json"; source=AppConfig()
            for key, value in {"sdk_directory":"custom", "host":"h", "channel":4, "timeout_s":3.0, "maximum_field_t":5.0, "minimum_temperature_k":1.2, "maximum_temperature_k":4.0, "poll_interval_s":.25}.items(): setattr(source.attodry2100,key,value)
            for key, value in {"start_field_t":-1.0, "stop_field_t":1.0, "point":"p5n2", "settle_timeout_s":9.0, "operation_timeout_s":10.0, "polling_interval_s":.1, "initial_voltage_settle_s":600.0, "voltage_settle_s":120.0, "temperature_control_enabled":True, "sample_target_k":20.0, "sample_ramp_rate_k_per_min":2.0, "temperature_tolerance_k":.05, "temperature_stable_s":5.0, "temperature_timeout_s":600.0}.items(): setattr(source.mcd2100,key,value)
            source.save(path); restored=AppConfig(); restored.load(path)
            self.assertEqual(asdict(restored.attodry2100), asdict(source.attodry2100)); self.assertEqual(asdict(restored.mcd2100), asdict(source.mcd2100))
            self.assertTrue(restored.mcd2100.temperature_control_enabled)
            self.assertEqual(restored.mcd2100.sample_target_k, 20.0)
            self.assertEqual(restored.mcd2100.point, "p5n2")
            self.assertEqual(restored.mcd2100.initial_voltage_settle_s, 600.0)
            self.assertEqual(restored.mcd2100.voltage_settle_s, 120.0)
    def test_2100_sections_round_trip_and_legacy_defaults(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "config.json"
            source = AppConfig()
            source.attodry2100.sdk_directory = "sdk"
            source.attodry2100.host = "example"
            source.attodry2100.channel = 3
            source.attodry2100.maximum_field_t = None
            source.mcd2100.operation_timeout_s = 42.0
            source.save(path)
            restored = AppConfig(); restored.load(path)
            self.assertEqual(restored.attodry2100.sdk_directory, "sdk")
            self.assertEqual(restored.attodry2100.channel, 3)
            self.assertIsNone(restored.attodry2100.maximum_field_t)
            self.assertEqual(restored.mcd2100.operation_timeout_s, 42.0)
            path.write_text("{}", encoding="utf-8")
            legacy = AppConfig(); legacy.load(path)
            self.assertEqual(legacy.attodry2100.channel, 0)
            self.assertEqual(legacy.mcd2100.start_field_t, -2.0)
    def test_round_trip_includes_versioned_session(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "config.json"
            source = AppConfig()
            source.lf6.center_nm = 777.5
            source.smu.compliance_by_addr = {
                "GPIB0::9::INSTR": {
                    "curr": 6e-7,
                    "curr_range": 100e-6,
                    "volt": 20.0,
                },
                "GPIB0::11::INSTR": {"curr": 9e-7, "volt": 30.0},
            }
            source.session.active_tab = "power_sweep"
            source.session.sample_id = "shared-sample"
            source.session.panels = {
                "power_sweep": {
                    "positions": "(1, 2, 3)",
                    "return_zero": True,
                }
            }

            source.save(path)

            restored = AppConfig()
            restored.load(path)
            self.assertEqual(restored.lf6.center_nm, 777.5)
            self.assertEqual(
                restored.smu.compliance_by_addr["GPIB0::11::INSTR"]["volt"],
                30.0,
            )
            self.assertAlmostEqual(
                restored.smu.compliance_by_addr["GPIB0::9::INSTR"]["curr_range"],
                100e-6,
            )
            self.assertEqual(restored.session.schema_version, 2)
            self.assertEqual(restored.session.active_tab, "power_sweep")
            self.assertEqual(restored.session.sample_id, "shared-sample")
            self.assertEqual(
                restored.session.panels["power_sweep"]["positions"],
                "(1, 2, 3)",
            )

    def test_save_is_atomic_and_leaves_no_temporary_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "config.json"
            config = AppConfig()
            config.save(path)

            data = json.loads(path.read_text(encoding="utf-8"))
            self.assertIn("session", data)
            self.assertEqual(
                list(Path(tmp).glob(f".{path.name}.*.tmp")),
                [],
            )

    def test_corrupt_or_wrong_shaped_sections_keep_defaults(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "config.json"
            path.write_text(
                json.dumps(
                    {
                        "lf6": "not a mapping",
                        "rotation": "not a mapping",
                        "session": {
                            "active_tab": 42,
                            "panels": {"good": {"value": 1}, "bad": []},
                        },
                    }
                ),
                encoding="utf-8",
            )

            config = AppConfig()
            config.load(path)
            self.assertEqual(config.lf6.center_nm, 860.0)
            self.assertEqual(config.session.active_tab, "dual_gate")
            self.assertEqual(config.session.panels, {"good": {"value": 1}})

            path.write_text("{broken", encoding="utf-8")
            config.load(path)
            self.assertEqual(config.lf6.center_nm, 860.0)

            path.write_text("[]", encoding="utf-8")
            config.load(path)
            self.assertEqual(config.session.active_tab, "dual_gate")


if __name__ == "__main__":
    unittest.main()
