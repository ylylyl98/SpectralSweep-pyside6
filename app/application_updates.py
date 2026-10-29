"""Offline release validation and upgrade policy for published Windows builds.

This module never downloads, installs, restarts or communicates with devices.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
import json
from pathlib import Path
import re
import sys
from urllib.parse import urlsplit

REPOSITORY = "ylylyl98/SpectralSweep-pyside6"
RELEASES_URL = f"https://github.com/{REPOSITORY}/releases"
RELEASES_API = f"https://api.github.com/repos/{REPOSITORY}/releases?per_page=50"
LATEST_RELEASE_API = f"https://api.github.com/repos/{REPOSITORY}/releases/latest"
MAX_RESPONSE_BYTES = 2 * 1024 * 1024
_BUILD = re.compile(r"build-([1-9][0-9]*)-([0-9a-f]{7,40})\Z")
_STABLE = re.compile(r"v(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\Z")
_COMMIT = re.compile(r"[0-9a-f]{40}\Z")


class UpdateProtocolError(ValueError):
    pass


def _version_key(tag):
    if not isinstance(tag, str) or len(tag) > 120:
        return None
    if match := _BUILD.fullmatch(tag):
        return "preview", (int(match[1]),)
    if match := _STABLE.fullmatch(tag):
        return "stable", tuple(int(part) for part in match.groups())
    return None


def _date(value):
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        return parsed if parsed.tzinfo is not None else None
    except (ValueError, TypeError, AttributeError):
        return None


@dataclass(frozen=True)
class InstalledVersion:
    version: str
    commit: str
    channel: str
    build_number: int = 0
    built_at: str = ""
    packaged: bool = False

    @property
    def can_check(self):
        key = _version_key(self.version)
        return bool(self.packaged and key and key[0] == self.channel
                    and isinstance(self.commit, str) and _COMMIT.fullmatch(self.commit)
                    and (self.channel != "preview"
                         or self.commit.startswith(_BUILD.fullmatch(self.version)[2])))


@dataclass(frozen=True)
class UpdateRelease:
    tag: str
    name: str
    html_url: str
    setup_url: str
    setup_size: int
    body: str
    published_at: str


def read_installed_version(executable=None, frozen=None):
    packaged = bool(getattr(sys, "frozen", False) if frozen is None else frozen)
    unknown = InstalledVersion("unknown" if packaged else "source", "", "unknown", packaged=packaged)
    if not packaged:
        return unknown
    try:
        path = Path(executable or sys.executable).resolve().with_name("version.json")
        if path.stat().st_size > 16384:
            return unknown
        data = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(data, dict) or data.get("repository", REPOSITORY) != REPOSITORY:
            return unknown
        version = data.get("version", "")
        key = _version_key(version)
        channel = data.get("channel", key[0] if key else "unknown")
        number = data.get("build_number", key[1][0] if key and key[0] == "preview" else 0)
        if isinstance(number, bool) or not isinstance(number, int) or number < 0:
            return unknown
        current = InstalledVersion(version, data.get("commit", ""), channel,
                                   number, data.get("built_at", ""), True)
        return current if current.can_check else unknown
    except (OSError, UnicodeError, ValueError, TypeError, RecursionError):
        return unknown


def decode_releases(payload):
    if len(payload) > MAX_RESPONSE_BYTES:
        raise UpdateProtocolError("The release response exceeded the size limit.")
    try:
        result = json.loads(payload.decode("utf-8"))
    except (UnicodeError, ValueError, RecursionError) as exc:
        raise UpdateProtocolError("The release server returned invalid JSON.") from exc
    if not isinstance(result, list):
        raise UpdateProtocolError("The release server did not return a release list.")
    return result


def _official_url(value, path):
    if not isinstance(value, str):
        return False
    try:
        parsed = urlsplit(value)
        return (parsed.scheme == "https" and parsed.netloc == "github.com"
                and parsed.path == path and not parsed.query and not parsed.fragment
                and value == "https://github.com" + path)
    except ValueError:
        return False


def _candidate(data):
    if not isinstance(data, dict) or data.get("draft") is not False:
        return None
    tag = data.get("tag_name")
    key = _version_key(tag)
    if not key or data.get("prerelease") is not (key[0] == "preview"):
        return None
    date = _date(data.get("published_at"))
    if date is None or not _official_url(data.get("html_url"), f"/{REPOSITORY}/releases/tag/{tag}"):
        return None
    filename = f"SpectralSweep-{tag}-windows-x64-Setup.exe"
    assets = data.get("assets")
    if not isinstance(assets, list):
        return None
    for asset in assets:
        if not isinstance(asset, dict) or asset.get("name") != filename or asset.get("state") != "uploaded":
            continue
        size = asset.get("size")
        path = f"/{REPOSITORY}/releases/download/{tag}/{filename}"
        if (type(size) is not int or size <= 0
                or not _official_url(asset.get("browser_download_url"), path)):
            continue
        return UpdateRelease(tag, str(data.get("name") or tag)[:200], data["html_url"],
                             asset["browser_download_url"], size,
                             str(data.get("body") or "")[:60000], data["published_at"])
    return None


def select_update(releases, current, channel):
    if not current.can_check or channel not in {"stable", "preview"}:
        return None
    current_key = _version_key(current.version)
    by_domain = {"stable": [], "preview": []}
    for data in releases:
        candidate = _candidate(data)
        if candidate is None or candidate.tag == current.version:
            continue
        key = _version_key(candidate.tag)
        if channel == "stable" and key[0] != "stable":
            continue
        # A full target SHA or the build tag's SHA identifies identical code.
        commit = data.get("target_commitish", "")
        build_match = _BUILD.fullmatch(candidate.tag)
        if (commit == current.commit or
                (build_match and current.commit.startswith(build_match[2]))):
            continue
        if key[0] == current_key[0]:
            newer = key[1] > current_key[1]
        else:
            built_at = _date(current.built_at)
            newer = built_at is not None and _date(candidate.published_at) > built_at
        if newer:
            by_domain[key[0]].append(candidate)
    # Version order within a domain; publication time only compares domains.
    winners = [max(items, key=lambda r: _version_key(r.tag)[1])
               for items in by_domain.values() if items]
    return max(winners, key=lambda r: _date(r.published_at)) if winners else None
