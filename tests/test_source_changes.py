from pathlib import Path

import pytest
from PySide6.QtCore import QElapsedTimer
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QLineEdit

from ui.source_changes import SourceChangeTracker


@pytest.fixture
def tracker(tmp_path):
    app = QApplication.instance() or QApplication([])
    files = {
        "ui/page.py": "old = 1\n", "ui/shared.py": "shared = 1\n",
        "controllers/device.py": "device = 1\n",
        "ui/styles/application.qss": "QLabel { color: red; }\n",
    }
    for name, content in files.items():
        path = tmp_path / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
    tracker = SourceChangeTracker(tmp_path, {"page": ("ui/page.py", "ui/shared.py")},
                                  shared=("ui/shared.py",))
    yield tracker
    tracker.deleteLater()
    app.processEvents()


def write(tracker, name, text):
    path = tracker.root / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def test_classifies_scoped_reload_styles_and_restart(tracker):
    for name in ("ui/page.py", "ui/shared.py", "controllers/device.py"):
        write(tracker, name, "changed = 2\n")
    write(tracker, tracker.style_path, "QLabel { color: blue; }\n")
    tracker.refresh()
    pending = {item["path"]: item for item in tracker.pending()}
    assert pending["ui/page.py"]["pages"] == ["page"]
    assert not pending["ui/page.py"]["restart"]
    assert pending["ui/shared.py"]["restart"]
    assert pending["controllers/device.py"]["restart"]
    assert pending[tracker.style_path]["styles"]


def test_success_marks_only_the_loaded_page_and_keeps_shared_restart(tracker):
    write(tracker, "ui/page.py", "old = 2\n")
    write(tracker, "ui/shared.py", "shared = 2\n")
    loaded = tracker.snapshot(tracker.groups["page"])
    tracker.mark_page_applied("page", loaded)
    assert [item["path"] for item in tracker.pending()] == ["ui/shared.py"]


def test_edits_during_reload_are_not_falsely_acknowledged(tracker):
    write(tracker, "ui/page.py", "old = 2\n")
    loaded = tracker.snapshot(tracker.groups["page"])
    write(tracker, "ui/page.py", "old = 3\n")
    tracker.mark_page_applied("page", loaded)
    assert [item["path"] for item in tracker.pending()] == ["ui/page.py"]


def test_reverting_shared_source_after_partial_reload_keeps_stale_page_visible(tracker):
    write(tracker, "ui/shared.py", "shared = 2\n")
    tracker.mark_page_applied("page", tracker.snapshot(tracker.groups["page"]))
    assert tracker.pending()[0]["restart"]
    assert tracker.pending()[0]["pages"] == []  # This page already has the edit.
    write(tracker, "ui/shared.py", "shared = 1\n")
    tracker.refresh()
    assert tracker.pending() == [{"path": "ui/shared.py", "pages": ["page"],
                                  "styles": False, "restart": False}]


def test_failed_reload_and_file_deletion_remain_pending(tracker):
    write(tracker, "ui/page.py", "syntax error\n")
    tracker.refresh()  # No successful mark_page_applied call.
    assert len(tracker.pending()) == 1
    (tracker.root / "ui/page.py").unlink()
    tracker.refresh()
    assert [item["path"] for item in tracker.pending()] == ["ui/page.py"]
    write(tracker, "ui/page.py", "old = 1\n")
    tracker.refresh()
    assert not tracker.pending()


def test_new_python_file_requires_restart(tracker):
    write(tracker, "utils/new_helper.py", "value = 1\n")
    tracker.refresh()
    assert tracker.pending() == [{"path": "utils/new_helper.py", "pages": [],
                                  "styles": False, "restart": True}]


@pytest.mark.parametrize("name", ["lf6_automation.py", "iv_automation.py", "TLPMX.py",
                                  "CRYO2100/client.py", "tools/launch_app.py"])
def test_legacy_drivers_and_launcher_are_not_omitted(tracker, name):
    write(tracker, name, "value = 1\n")
    tracker.refresh()
    assert tracker.pending() == [{"path": name, "pages": [], "styles": False, "restart": True}]


def test_style_acknowledges_only_successfully_applied_source(tracker):
    source = "QLabel { color: blue; }\n"
    write(tracker, tracker.style_path, source)
    tracker.mark_styles_applied(source)
    assert not tracker.pending()
    write(tracker, tracker.style_path, "QLabel { color:")
    tracker.refresh()
    assert tracker.pending()[0]["styles"]


def test_quick_style_initial_acknowledgement_has_no_phantom_changes(tracker):
    name = "ui/styles/quick_application.qss"
    source = "QLabel { color: green; }\n"
    write(tracker, name, source)
    assets = SourceChangeTracker(tracker.root, {}, style_path=tracker.root / name, parent=tracker)
    assets.mark_styles_applied(source)
    assert assets.style_path == name
    assert not assets.pending()


def test_style_switch_tracks_active_qss_and_acknowledges_only_its_source(tracker):
    quick = "ui/styles/quick_application.qss"
    quick_source = "QLabel { color: green; }\n"
    write(tracker, quick, quick_source)
    assets = SourceChangeTracker(tracker.root, {}, parent=tracker)
    # Quick mode applies its stylesheet after constructing the tracker.
    assets.mark_styles_applied(quick_source, tracker.root / quick)
    assert not assets.pending()
    write(tracker, quick, "QLabel { color: purple; }\n")
    write(tracker, tracker.style_path, "QLabel { color: orange; }\n")
    qml = "ui/qml/Page.qml"
    write(tracker, qml, "Item {}\n")
    assets.refresh()
    pending = {item["path"]: item for item in assets.pending()}
    assert pending[quick] == {"path": quick, "pages": [], "styles": True, "restart": False}
    assert pending[tracker.style_path]["restart"] and not pending[tracker.style_path]["styles"]
    assets.mark_styles_applied("QLabel { color: purple; }\n", quick)
    assert {item["path"] for item in assets.pending()} == {tracker.style_path, qml}


def test_external_stylesheets_do_not_fingerprint_repository_styles(tracker):
    external = tracker.root.parent / "external.qss"
    assets = SourceChangeTracker(tracker.root, {}, style_path=external, parent=tracker)
    assets.mark_styles_applied("external style")
    assert assets.style_path is None
    assert None not in assets._baseline
    assert not assets.pending()
    write(tracker, tracker.style_path, "QLabel { color: orange; }\n")
    tracker.mark_styles_applied("external style", external)
    assert tracker.style_path is None
    assert tracker.pending() == [{"path": "ui/styles/application.qss", "pages": [],
                                  "styles": False, "restart": True}]


def test_watcher_survives_atomic_replacement(tracker):
    replacement = tracker.root / "ui/replacement.tmp"
    replacement.write_text("old = 2\n", encoding="utf-8")
    replacement.replace(tracker.root / "ui/page.py")
    timer = QElapsedTimer()
    timer.start()
    while not tracker.pending() and timer.elapsed() < 5000:
        QTest.qWait(25)
    assert [item["path"] for item in tracker.pending()] == ["ui/page.py"]


@pytest.mark.parametrize("name", ["ui/qml/Page.qml", "ui/qml/qmldir",
                                  "ui/qml/helpers.js", "ui/qml/helpers.mjs"])
def test_quick_asset_edits_and_deletions_require_restart_even_after_page_reload(tracker, name):
    write(tracker, name, "original source\n")
    # Even an accidentally registered asset cannot be acknowledged by a
    # Python page reload while the shared QML engine still has cached code.
    assets = SourceChangeTracker(tracker.root, {"page": ("ui/page.py", name)}, parent=tracker)
    expected = [{"path": name, "pages": [], "styles": False, "restart": True}]
    write(tracker, name, "updated source\n")
    assets.refresh()
    assert assets.pending() == expected
    assets.mark_page_applied("page", assets.snapshot(assets.groups["page"]))
    assets.mark_styles_applied((tracker.root / tracker.style_path).read_text(encoding="utf-8"))
    assert assets.pending() == expected
    (tracker.root / name).unlink()
    assets.refresh()
    assert assets.pending() == expected
    write(tracker, name, "original source\n")
    assets.refresh()
    assert not assets.pending()


@pytest.mark.parametrize("name", ["ui/qml/Page.qml", "ui/qml/qmldir",
                                  "ui/qml/helpers.js", "ui/qml/helpers.mjs"])
def test_new_quick_assets_require_restart(tracker, name):
    write(tracker, name, "new source\n")
    tracker.refresh()
    assert tracker.pending() == [{"path": name, "pages": [], "styles": False, "restart": True}]


def test_quick_asset_watcher_survives_atomic_replacement(tracker):
    name = "ui/qml/Page.qml"
    write(tracker, name, "original source\n")
    assets = SourceChangeTracker(tracker.root, {}, parent=tracker)
    replacement = tracker.root / "ui/qml/replacement.tmp"
    replacement.write_text("updated source\n", encoding="utf-8")
    replacement.replace(tracker.root / name)
    timer = QElapsedTimer()
    timer.start()
    while not assets.pending() and timer.elapsed() < 5000:
        QTest.qWait(25)
    assert assets.pending() == [{"path": name, "pages": [], "styles": False, "restart": True}]


def test_refresh_reads_only_changed_source_files(tracker, monkeypatch):
    from ui import source_changes
    calls = []
    original = source_changes.source_digest
    def counted(path):
        calls.append(path)
        return original(path)
    monkeypatch.setattr(source_changes, "source_digest", counted)
    tracker.refresh()
    assert not calls
    write(tracker, "ui/page.py", "old = 100\n")
    tracker.refresh()
    assert calls == [tracker.root / "ui/page.py"]


def test_file_event_rehashes_even_when_signature_is_unchanged(tracker, monkeypatch):
    from ui import source_changes
    path = tracker.root / "ui/page.py"
    monkeypatch.setattr(source_changes, "source_digest", lambda _path: "new-content-same-stat")
    tracker._schedule(str(path))
    tracker.refresh()
    assert tracker.pending() == [{"path": "ui/page.py", "pages": ["page"],
                                  "styles": False, "restart": False}]


def test_unreadable_source_does_not_reuse_stale_cached_success(tracker, monkeypatch):
    from ui import source_changes
    original = source_changes.source_digest
    path = tracker.root / "ui/page.py"
    monkeypatch.setattr(source_changes, "source_digest", lambda item:
                        "missing-or-unreadable" if item == path else original(item))
    tracker._schedule(str(path))
    tracker.refresh()
    assert tracker.pending()
    tracker.refresh()
    assert tracker.pending()  # A matching stat cannot conceal the read failure.
    monkeypatch.setattr(source_changes, "source_digest", original)
    tracker.refresh()
    assert not tracker.pending()


def test_sample_binder_replacement_preserves_draft_and_drops_old_edit():
    from ui.main_window import _SharedSampleIdBinder
    app = QApplication.instance() or QApplication([])
    old, other, new = QLineEdit(), QLineEdit(), QLineEdit()
    binder = _SharedSampleIdBinder([old, other], initial="sample", commit_on_edit=True)
    new.setText("unfinished draft")
    binder.replace_edit(old, new)
    assert binder.value == "sample"
    assert new.text() == "unfinished draft"
    binder.commit("next sample")
    assert old.text() == "sample"
    assert new.text() == other.text() == "next sample"
    for widget in (old, other, new):
        widget.deleteLater()
    binder.deleteLater()
    app.processEvents()
