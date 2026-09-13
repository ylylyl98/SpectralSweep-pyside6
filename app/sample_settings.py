"""Per-sample editable setup profiles.

The store deliberately contains only JSON-compatible data.  The caller owns
the Qt widgets and decides which panel state is safe to restore; this keeps
loading a profile free of controller or hardware side effects.
"""

from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timezone
from typing import Any, Mapping, MutableMapping, Optional


class SampleSettingsStore:
    """Manage versioned Sample ID -> setup snapshots in a session mapping."""

    SCHEMA_VERSION = 1

    def __init__(self, backing: Optional[MutableMapping[str, Any]] = None):
        self._backing = backing if backing is not None else {}
        profiles = self._backing.get("sample_profiles")
        if not isinstance(profiles, dict):
            profiles = {}
            self._backing["sample_profiles"] = profiles
        self._profiles = profiles

    @staticmethod
    def normalize_id(sample_id: object) -> str:
        return str(sample_id or "").strip()

    @staticmethod
    def _stamp() -> str:
        return datetime.now(timezone.utc).isoformat(timespec="seconds")

    @property
    def profiles(self) -> dict[str, dict[str, Any]]:
        return deepcopy(self._profiles)

    def ids(self, query: str = "") -> list[str]:
        query = self.normalize_id(query).casefold()
        ids = [key for key, value in self._profiles.items() if isinstance(value, dict)]
        if query:
            ids = [key for key in ids if query in key.casefold()]
        return sorted(ids, key=lambda key: (
            str(self._profiles.get(key, {}).get("updated_at", "")), key.casefold()
        ), reverse=True)

    def load(self, sample_id: object) -> Optional[dict[str, Any]]:
        key = self.normalize_id(sample_id)
        profile = self._profiles.get(key)
        if not isinstance(profile, dict):
            return None
        state = profile.get("state")
        return deepcopy(state) if isinstance(state, dict) else None

    def save(self, sample_id: object, state: Mapping[str, Any]) -> bool:
        key = self.normalize_id(sample_id)
        if not key or not isinstance(state, Mapping):
            return False
        self._profiles[key] = {
            "schema_version": self.SCHEMA_VERSION,
            "updated_at": self._stamp(),
            "state": deepcopy(dict(state)),
        }
        return True

    def duplicate(self, source_id: object, target_id: object) -> bool:
        source = self.load(source_id)
        source_key = self.normalize_id(source_id)
        target = self.normalize_id(target_id)
        if source is None or not target or target == source_key or target in self._profiles:
            return False
        return self.save(target, source)

    def migrate_legacy(self, sample_id: object, legacy_state: Mapping[str, Any]) -> bool:
        key = self.normalize_id(sample_id)
        if not key or self.load(key) is not None or not isinstance(legacy_state, Mapping):
            return False
        return self.save(key, legacy_state)
