from __future__ import annotations

import copy
import unittest

from app.sample_settings import SampleSettingsStore


class SampleSettingsStoreTests(unittest.TestCase):
    def test_profiles_are_isolated_and_return_deep_copies(self):
        store = SampleSettingsStore()
        state = {"panels": {"dual_gate": {"draft_batch": [{"Vbg": ""}]}}}
        store.save("YZ365", state)

        loaded = store.load("YZ365")
        loaded["panels"]["dual_gate"]["draft_batch"][0]["Vbg"] = "partial"

        self.assertEqual(store.load("YZ365")["panels"]["dual_gate"]["draft_batch"][0]["Vbg"], "")
        self.assertIsNone(store.load("YZ366"))

    def test_duplicate_copies_current_profile_without_aliasing(self):
        store = SampleSettingsStore()
        store.save("YZ365", {"panels": {"dual_gate": {"value": 1}}})

        self.assertTrue(store.duplicate("YZ365", "YZ366"))
        store.load("YZ366")["panels"]["dual_gate"]["value"] = 2
        self.assertEqual(store.load("YZ365")["panels"]["dual_gate"]["value"], 1)

    def test_duplicate_does_not_overwrite_existing_profile(self):
        store = SampleSettingsStore()
        store.save("YZ365", {"value": 1})
        store.save("YZ366", {"value": 2})

        self.assertFalse(store.duplicate("YZ365", "YZ366"))
        self.assertEqual(store.load("YZ366")["value"], 2)

    def test_migrates_legacy_session_once_to_current_sample(self):
        store = SampleSettingsStore()
        legacy = {"active_tab": "dual_gate", "panels": {"dual_gate": {"value": 7}}}

        self.assertTrue(store.migrate_legacy("YZ365", legacy))
        self.assertFalse(store.migrate_legacy("YZ365", {"panels": {"value": 8}}))
        self.assertEqual(store.load("YZ365")["panels"]["dual_gate"]["value"], 7)


if __name__ == "__main__":
    unittest.main()
