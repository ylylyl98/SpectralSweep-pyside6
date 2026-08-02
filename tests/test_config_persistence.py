from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from utils.config import AppConfig


class ConfigPersistenceTests(unittest.TestCase):
    def test_round_trip_includes_versioned_session(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "config.json"
            source = AppConfig()
            source.lf6.center_nm = 777.5
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
