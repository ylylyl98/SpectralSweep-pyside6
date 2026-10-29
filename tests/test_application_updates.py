"""Offline release protocol and upgrade policy tests; no Qt or network services."""
from dataclasses import FrozenInstanceError
import json
from pathlib import Path

import pytest

from app.application_updates import (
    InstalledVersion, MAX_RESPONSE_BYTES, REPOSITORY, UpdateProtocolError,
    decode_releases, read_installed_version, select_update,
)


SHA = "a" * 40
OTHER_SHA = "b" * 40
OLD_DATE = "2026-10-27T12:00:00Z"
CURRENT_DATE = "2026-10-28T12:00:00Z"
NEW_DATE = "2026-10-29T12:00:00Z"


def installed(version="build-9-aaaaaaaa", *, built_at=CURRENT_DATE, packaged=True):
    preview = version.startswith("build-")
    return InstalledVersion(version=version, commit=SHA,
                            channel="preview" if preview else "stable",
                            build_number=9 if preview else 0,
                            built_at=built_at, packaged=packaged)


def release(tag="build-10-bbbbbbbb", *, prerelease=True, published_at=NEW_DATE):
    name = f"SpectralSweep-{tag}-windows-x64-Setup.exe"
    return {
        "tag_name": tag, "name": f"SpectralSweep {tag}", "draft": False,
        "prerelease": prerelease, "target_commitish": OTHER_SHA,
        "html_url": f"https://github.com/{REPOSITORY}/releases/tag/{tag}",
        "published_at": published_at, "body": "Verified Windows release.",
        "assets": [{"name": name, "state": "uploaded", "size": 123456,
                    "browser_download_url": f"https://github.com/{REPOSITORY}/releases/download/{tag}/{name}"}],
    }


def test_installed_version_is_immutable():
    current = installed()
    assert current.can_check
    with pytest.raises(FrozenInstanceError):
        current.version = "build-100-aaaaaaaa"


@pytest.mark.parametrize("version,channel,packaged,expected", [
    ("build-9-aaaaaaaa", "preview", True, True),
    ("build-9-aaaaaaa", "preview", True, True),
    ("build-9-" + SHA, "preview", True, True),
    ("v1.2.3", "stable", True, True),
    ("build-9-aaaaaaaa", "preview", False, False),
    ("v1.2.3", "stable", False, False),
    ("local-ci-validation", "preview", True, False),
    ("build-9-aaaaaa", "preview", True, False),
    ("build-9-" + SHA + "a", "preview", True, False),
    ("build-nine-aaaaaaaa", "preview", True, False),
    ("v1.2", "stable", True, False),
    ("v1.2.3-rc1", "stable", True, False),
    ("v1.2.3", "preview", True, False),
    ("build-9-aaaaaaaa", "stable", True, False),
])
def test_can_check_requires_a_packaged_supported_version(version, channel, packaged, expected):
    current = InstalledVersion(version, SHA, channel, build_number=9, packaged=packaged)
    assert current.can_check is expected


def write_metadata(tmp_path, **overrides):
    executable = tmp_path / "SpectralSweep.exe"
    executable.touch()
    metadata = {"version": "build-9-aaaaaaaa", "commit": SHA,
                "repository": REPOSITORY, "channel": "preview", "build_number": 9,
                "built_at": CURRENT_DATE}
    metadata.update(overrides)
    (tmp_path / "version.json").write_text(json.dumps(metadata), encoding="utf-8")
    return executable


def test_read_installed_version_uses_executable_sibling(tmp_path, monkeypatch):
    executable = write_metadata(tmp_path)
    fake_cwd = tmp_path / "working-directory"
    fake_cwd.mkdir()
    (fake_cwd / "version.json").write_text('{"version":"v99.0.0"}', encoding="utf-8")
    monkeypatch.chdir(fake_cwd)
    current = read_installed_version(executable, frozen=True)
    assert current.can_check and current.packaged
    assert (current.version, current.commit, current.channel, current.build_number) == (
        "build-9-aaaaaaaa", SHA, "preview", 9)
    assert current.built_at == CURRENT_DATE


def test_source_run_does_not_adopt_a_packaged_metadata_file(tmp_path, monkeypatch):
    executable = write_metadata(tmp_path)
    monkeypatch.chdir(tmp_path)
    current = read_installed_version(executable, frozen=False)
    assert not current.packaged and not current.can_check


@pytest.mark.parametrize("overrides", [
    {"repository": "someone-else/SpectralSweep-pyside6"},
    {"commit": OTHER_SHA},
    {"version": "local"},
])
def test_untrusted_or_inconsistent_metadata_cannot_check(tmp_path, overrides):
    executable = write_metadata(tmp_path, **overrides)
    assert not read_installed_version(executable, frozen=True).can_check


@pytest.mark.parametrize("content", [None, "{bad-json", "[]", "null"])
def test_missing_or_malformed_metadata_does_not_crash(tmp_path, content):
    executable = tmp_path / "SpectralSweep.exe"
    if content is not None:
        (tmp_path / "version.json").write_text(content, encoding="utf-8")
    assert not read_installed_version(executable, frozen=True).can_check


def test_decode_release_list_preserves_release_data():
    data = [release()]
    assert decode_releases(json.dumps(data).encode("utf-8")) == data
    assert decode_releases(b"[]") == []


@pytest.mark.parametrize("payload", [b"{}", b"null", b'"text"', b"[", b"\xff", b"1"])
def test_decode_releases_rejects_invalid_json_protocol(payload):
    with pytest.raises(UpdateProtocolError):
        decode_releases(payload)


def test_decode_releases_enforces_response_size_limit():
    assert MAX_RESPONSE_BYTES == 2 * 1024 * 1024
    with pytest.raises(UpdateProtocolError):
        decode_releases(b" " * (MAX_RESPONSE_BYTES + 1))


def test_build_numbers_are_compared_as_integers_and_result_contains_setup():
    candidate = release()
    result = select_update([candidate], installed(), "preview")
    assert result is not None
    assert result.tag == candidate["tag_name"]
    assert result.name == candidate["name"]
    assert result.html_url == candidate["html_url"]
    assert result.setup_url == candidate["assets"][0]["browser_download_url"]
    assert result.setup_size == candidate["assets"][0]["size"]
    assert result.body == candidate["body"]
    assert result.published_at == NEW_DATE


def test_stable_versions_are_compared_numerically_and_ignore_previews():
    newer = release("v1.10.0", prerelease=False)
    older = release("v1.9.9", prerelease=False)
    result = select_update([release("build-100-bbbbbbbb"), older, newer],
                           installed("v1.9.0"), "stable")
    assert result is not None and result.tag == "v1.10.0"


def test_stable_channel_does_not_offer_a_prerelease_semver():
    assert select_update([release("v9.0.0", prerelease=True)], installed("v1.0.0"), "stable") is None


@pytest.mark.parametrize("tag", ["build-9-aaaaaaaa", "build-8-bbbbbbbb"])
def test_same_tag_or_build_downgrade_is_not_an_update(tag):
    assert select_update([release(tag)], installed(), "preview") is None


@pytest.mark.parametrize("tag", ["v1.9.0", "v1.8.9"])
def test_same_stable_version_or_downgrade_is_not_an_update(tag):
    assert select_update([release(tag, prerelease=False)], installed("v1.9.0"), "stable") is None


def test_same_commit_is_not_offered_under_a_new_tag():
    candidate = release("v2.0.0", prerelease=False)
    candidate["target_commitish"] = SHA
    assert select_update([candidate], installed("v1.0.0"), "stable") is None


@pytest.mark.parametrize("published_at,built_at,expected", [
    (NEW_DATE, CURRENT_DATE, True),
    (CURRENT_DATE, CURRENT_DATE, False),
    (OLD_DATE, CURRENT_DATE, False),
    (NEW_DATE, "", False),
])
def test_cross_domain_updates_require_a_strictly_later_publication(published_at, built_at, expected):
    candidate = release("v1.0.0", prerelease=False, published_at=published_at)
    result = select_update([candidate], installed(built_at=built_at), "preview")
    assert (result is not None) is expected


def test_preview_channel_can_offer_a_later_build_to_a_stable_install():
    result = select_update([release()], installed("v1.0.0"), "preview")
    assert result is not None and result.tag == "build-10-bbbbbbbb"


@pytest.mark.parametrize("published_at", ["", "not-a-date", "2026-99-99T00:00:00Z"])
def test_invalid_publication_dates_are_ignored(published_at):
    assert select_update([release(published_at=published_at)], installed(), "preview") is None


def test_drafts_and_releases_without_usable_setup_are_ignored():
    draft = release(); draft["draft"] = True
    missing = release(); missing["assets"] = []
    assert select_update([draft, missing], installed(), "preview") is None


@pytest.mark.parametrize("field,value", [
    ("state", "new"), ("size", 0), ("size", -1),
    ("name", "SpectralSweep-build-10-bbbbbbbb-windows-arm64-Setup.exe"),
    ("name", "SpectralSweep-Setup.exe"),
])
def test_invalid_setup_assets_are_ignored(field, value):
    candidate = release(); candidate["assets"][0][field] = value
    assert select_update([candidate], installed(), "preview") is None


@pytest.mark.parametrize("url", [
    "http://github.com/{repo}/releases/tag/{tag}",
    "https://github.com.evil.test/{repo}/releases/tag/{tag}",
    "https://user@github.com/{repo}/releases/tag/{tag}",
    "https://github.com:443/{repo}/releases/tag/{tag}",
    "https://github.com/other/repository/releases/tag/{tag}",
    "https://github.com/{repo}/releases/tag/{tag}?download=1",
    "https://github.com/{repo}/releases/tag/{tag}#fragment",
    "https://github.com/{repo}/releases/tag/%2e%2e/{tag}",
])
def test_untrusted_release_page_urls_are_rejected(url):
    candidate = release()
    candidate["html_url"] = url.format(repo=REPOSITORY, tag=candidate["tag_name"])
    assert select_update([candidate], installed(), "preview") is None


@pytest.mark.parametrize("url", [
    "http://github.com/{repo}/releases/download/{tag}/{name}",
    "https://github.com.evil.test/{repo}/releases/download/{tag}/{name}",
    "https://user:password@github.com/{repo}/releases/download/{tag}/{name}",
    "https://github.com:443/{repo}/releases/download/{tag}/{name}",
    "https://github.com/other/repository/releases/download/{tag}/{name}",
    "https://github.com/{repo}/releases/download/{tag}/{name}?download=1",
    "https://github.com/{repo}/releases/download/{tag}/{name}#fragment",
    "https://github.com/{repo}/releases/download/{tag}/%2e%2e/{name}",
])
def test_untrusted_setup_urls_are_rejected(url):
    candidate = release()
    candidate["assets"][0]["browser_download_url"] = url.format(
        repo=REPOSITORY, tag=candidate["tag_name"], name=candidate["assets"][0]["name"])
    assert select_update([candidate], installed(), "preview") is None


def test_invalid_installed_version_never_generates_an_update_notice():
    assert select_update([release()], installed(packaged=False), "preview") is None


def test_unordered_build_releases_select_the_highest_run_number():
    result = select_update([release("build-10-bbbbbbbb"), release("build-8-bbbbbbbb"),
                            release("build-11-bbbbbbbb")], installed(), "preview")
    assert result is not None and result.tag == "build-11-bbbbbbbb"


def test_incomplete_release_does_not_hide_a_later_usable_release():
    incomplete = release("build-11-bbbbbbbb")
    incomplete["assets"] = []
    result = select_update([incomplete, release()], installed(), "preview")
    assert result is not None and result.tag == "build-10-bbbbbbbb"


def test_malformed_numeric_tag_does_not_prevent_a_valid_update():
    malformed = release()
    malformed["tag_name"] = "build-" + "1" * 5000 + "-bbbbbbbb"
    result = select_update([malformed, release()], installed(), "preview")
    assert result is not None and result.tag == "build-10-bbbbbbbb"


def test_deeply_nested_metadata_is_treated_as_unknown(tmp_path):
    executable = tmp_path / "SpectralSweep.exe"
    (tmp_path / "version.json").write_text("[" * 6000 + "0" + "]" * 6000, encoding="utf-8")
    assert not read_installed_version(executable, frozen=True).can_check
