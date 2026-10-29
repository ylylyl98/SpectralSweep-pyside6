"""History Quick UI uses temporary records and never constructs a controller."""
import json
import os
import subprocess
import sys
import threading
import time
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ.setdefault("QT_QUICK_BACKEND", "software")

import pytest
from PySide6.QtCore import QObject, QPoint, Qt
from PySide6.QtGui import QFontDatabase, QFont
from PySide6.QtQuickWidgets import QQuickWidget
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication

from app.experiment_metadata import ExperimentHistory
from app.history_viewmodel import HistoryViewModel
from ui.quick_history import QuickHistoryWidget
from tests.quick_helpers import drain_qt_deletions, quick_application


@pytest.fixture(scope="module")
def app():
    app = quick_application()
    # Windows' offscreen platform has no system font database. Render real glyphs
    # in the screenshot check instead of accepting an image full of tofu boxes.
    font_path = Path(os.environ.get("WINDIR", "C:/Windows")) / "Fonts" / "segoeui.ttf"
    if not QFontDatabase.families() and font_path.exists():
        QFontDatabase.addApplicationFont(str(font_path))
        app.setFont(QFont("Segoe UI", 9))
    return app


def wait_until(predicate, timeout=4000):
    deadline = time.monotonic() + timeout / 1000
    while not predicate() and time.monotonic() < deadline:
        QTest.qWait(10)
    assert predicate()


@pytest.fixture
def history(tmp_path):
    history = ExperimentHistory(tmp_path / "history.sqlite")
    for sample, count in (("sample-A", 5), ("sample-B", 1)):
        for i in range(count):
            path = tmp_path / f"{sample}-{i}.json"
            journal = path.with_suffix(".events.jsonl")
            journal.write_text("\n".join(json.dumps({"event": f"ENTRY_{j:03d}"}) for j in range(41)), encoding="utf-8")
            metadata = {
                "experiment_id": f"{sample}-{i}", "device_id": sample,
                "experiment_type": "dual_gate_sweep", "started_utc": f"2026-10-26T10:0{i}:00Z",
                "status": "completed" if i != 3 else "failed", "metadata_path": str(path),
                "settings": {"loadable": {"center_nm": 800 + i, "large_unused": [1, 2]}},
                "event_log": {"path": journal.name},
            }
            path.write_text(json.dumps(metadata), encoding="utf-8")
            history.upsert(metadata)
    return history


def test_two_history_surfaces_keep_their_models_private(app, history, tmp_path):
    first = QuickHistoryWidget(history, tmp_path, "sample-A")
    second = QuickHistoryWidget(history, tmp_path, "sample-B")
    try:
        assert first.engine() is second.engine()
        assert first.rootObject().property("historyModel") is first.view_model
        assert second.rootObject().property("historyModel") is second.view_model
        wait_until(lambda: first.view_model.total == 5 and second.view_model.total == 1)
        first.shutdown()
        first.deleteLater()
        drain_qt_deletions()
        assert second.view_model.sampleId == "sample-B"
        assert second.rootObject().property("historyModel") is second.view_model
    finally:
        second.shutdown()
        second.deleteLater()
        drain_qt_deletions()


def test_sample_filter_search_paging_and_real_metadata(app, history, tmp_path):
    vm = HistoryViewModel(history, tmp_path, "sample-A", ["center_nm"], page_size=2)
    try:
        wait_until(lambda: vm.total == 5 and not vm.loading)
        assert vm.rows_model.rowCount() == 2
        assert vm.rows_model.records[0]["experiment_id"] == "sample-A-4"
        vm.changePage(1)
        wait_until(lambda: vm.page == 1 and not vm.loading)
        vm.selectRow(0)
        wait_until(lambda: vm.canOpen)
        assert "ENTRY_000" in vm.previewText and "ENTRY_019" in vm.previewText
        assert "ENTRY_020" not in vm.previewText
        vm.changeEventPage(1)
        wait_until(lambda: vm.eventPage == 1 and not vm.previewLoading)
        assert "ENTRY_020" in vm.previewText and "ENTRY_039" in vm.previewText
        vm.changeEventPage(1)
        wait_until(lambda: vm.eventPage == 2 and not vm.previewLoading)
        assert "ENTRY_040" in vm.previewText and not vm.eventHasNext
        vm.setEventQuery("ENTRY_040")
        wait_until(lambda: not vm.previewLoading and vm.eventPage == 0 and "ENTRY_040" in vm.previewText)
        assert "ENTRY_000" not in vm.previewText
        opened, loaded = [], []
        vm.metadataOpenRequested.connect(opened.append)
        vm.settingsContextLoaded.connect(lambda kind, settings: loaded.append((kind, settings)))
        vm.openMetadata()
        assert opened and Path(opened[0]).is_file()
        vm.requestSettings()
        wait_until(lambda: bool(loaded))
        assert loaded[0][0] == "dual_gate_sweep"
        assert loaded[0][1]["large_unused"] is None
        vm.setQuery("FAILED")
        wait_until(lambda: not vm.loading and vm.total == 1)
        assert vm.page == 0 and vm.selectedIndex == -1 and not vm.canLoad
        vm.setQuery("")
        vm.setSampleId("sample-B")
        wait_until(lambda: not vm.loading and vm.total == 1)
        assert vm.rows_model.records[0]["device_id"] == "sample-B"
    finally:
        vm.shutdown()


def test_debounce_cancels_old_list_and_never_displays_stale_sample(app, tmp_path):
    entered, release = threading.Event(), threading.Event()
    calls = []
    class SlowHistory:
        def query_page(self, sample, kind, **kwargs):
            calls.append((sample, kwargs["filter_text"]))
            if sample == "old":
                entered.set()
                release.wait(3)
            return {"rows": [{"experiment_id": sample}], "total": 1, "page": 0}
    vm = HistoryViewModel(SlowHistory(), tmp_path, "old")
    try:
        wait_until(entered.is_set)
        vm.setSampleId("new")
        wait_until(lambda: not vm.loading and vm.rows_model.records[0]["experiment_id"] == "new")
        release.set()
        QTest.qWait(50)
        assert vm.rows_model.records[0]["experiment_id"] == "new"
        vm.setQuery("a")
        vm.setQuery("ab")
        vm.setQuery("abc")
        wait_until(lambda: not vm.loading)
        assert [query for sample, query in calls if query] == ["abc"]
    finally:
        release.set()
        vm.shutdown()


def test_errors_empty_and_cancelled_preview_are_explicit(app, history, tmp_path, monkeypatch):
    import app.history_viewmodel as module
    vm = HistoryViewModel(history, tmp_path, "sample-A")
    release, entered = threading.Event(), threading.Event()
    def slow_preview(*args):
        entered.set()
        release.wait(3)
        return {"path": "old.json", "text": "STALE", "has_next": False}
    try:
        wait_until(lambda: vm.total == 5)
        monkeypatch.setattr(module, "load_preview", slow_preview)
        vm.selectRow(0)
        wait_until(entered.is_set)
        vm.setSampleId("absent")
        wait_until(lambda: not vm.loading)
        release.set()
        QTest.qWait(50)
        assert vm.total == 0 and vm.previewText == "" and not vm.canOpen
        assert vm.rows_model.rowCount() == 0
        def broken(*args, **kwargs):
            raise OSError("temporary database error")
        monkeypatch.setattr(history, "query_page", broken)
        vm.refresh()
        wait_until(lambda: not vm.loading)
        assert "temporary database error" in vm.errorText
        assert not vm.canLoad
    finally:
        release.set()
        vm.shutdown()


def test_metadata_error_and_settings_error_leave_retry_possible(app, history, tmp_path, monkeypatch):
    import app.history_viewmodel as module
    vm = HistoryViewModel(history, tmp_path, "sample-A")
    try:
        wait_until(lambda: vm.total == 5)
        original = module.load_preview
        def broken(*args, **kwargs):
            raise ValueError("invalid metadata")
        monkeypatch.setattr(module, "load_preview", broken)
        vm.selectRow(0)
        wait_until(lambda: not vm.previewLoading)
        assert "invalid metadata" in vm.errorText and not vm.canOpen
        monkeypatch.setattr(module, "load_preview", original)
        vm.selectRow(0)
        wait_until(lambda: vm.canLoad)
        monkeypatch.setattr(module, "load_settings", broken)
        vm.requestSettings()
        wait_until(lambda: not vm.settingsLoading)
        assert "invalid metadata" in vm.errorText and vm.canLoad
    finally:
        vm.shutdown()


def test_cancelled_settings_never_reaches_shell_after_sample_changes(app, history, tmp_path, monkeypatch):
    import app.history_viewmodel as module
    entered, release = threading.Event(), threading.Event()
    def delayed_settings(*args, **kwargs):
        entered.set()
        release.wait(3)
        return {"center_nm": 123}
    vm = HistoryViewModel(history, tmp_path, "sample-A")
    loaded = []
    vm.settingsLoaded.connect(loaded.append)
    try:
        wait_until(lambda: vm.total == 5)
        vm.selectRow(0)
        wait_until(lambda: vm.canLoad)
        monkeypatch.setattr(module, "load_settings", delayed_settings)
        vm.requestSettings()
        wait_until(entered.is_set)
        vm.setSampleId("sample-B")
        release.set()
        wait_until(lambda: not vm.loading)
        QTest.qWait(50)
        assert loaded == [] and not vm.settingsLoading
    finally:
        release.set()
        vm.shutdown()


def test_actual_qquickwidget_loads_renders_and_keyboard_selects(app, history, tmp_path):
    widget = QuickHistoryWidget(history, tmp_path, "sample-A", page_size=2)
    widget.resize(1060, 680)
    widget.show()
    try:
        wait_until(lambda: widget.view_model.total == 5)
        assert widget.status() == QQuickWidget.Status.Ready, [e.toString() for e in widget.errors()]
        assert widget.rootObject().objectName() == "HistoryPage"
        workflow = widget.rootObject().findChild(QObject, "HistoryWorkflow")
        assert workflow.property("currentText") == "Dual Gate"
        assert workflow.property("currentValue") == "dual_gate_sweep"
        runs = widget.rootObject().findChild(QObject, "HistoryRuns")
        assert runs is not None
        runs.forceActiveFocus()
        QTest.keyClick(widget, Qt.Key.Key_Down)
        wait_until(lambda: widget.view_model.selectedIndex == 0 and widget.view_model.canOpen)
        opened = []
        widget.metadataOpenRequested.connect(opened.append)
        open_button = widget.rootObject().findChild(QObject, "OpenHistoryMetadata")
        position = open_button.mapToScene(open_button.boundingRect().center())
        QTest.mouseClick(widget, Qt.MouseButton.LeftButton, pos=QPoint(round(position.x()), round(position.y())))
        assert len(opened) == 1
        widget.set_theme("light")
        QTest.qWait(50)
        light = widget.grabFramebuffer()
        assert not light.isNull()
        widget.set_theme("dark")
        QTest.qWait(50)
        dark = widget.grabFramebuffer()
        assert not dark.isNull()
        assert dark.pixelColor(3, 3).lightness() < light.pixelColor(3, 3).lightness()
        assert widget.qml_warnings == []
    finally:
        widget.close()
        widget.deleteLater()
        drain_qt_deletions()


@pytest.mark.skipif(os.name != "nt", reason="Windows native rendering check")
def test_native_windows_hidden_window_renders_real_fonts(tmp_path):
    script = r'''
import json,sys
from pathlib import Path
from PySide6.QtCore import Qt
from PySide6.QtGui import QFontDatabase
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication
from app.experiment_metadata import ExperimentHistory
from ui.quick_history import QuickHistoryWidget
root=Path(sys.argv[1])
app=QApplication([])
assert app.platformName() == 'windows'
assert 'Segoe UI' in QFontDatabase.families()
history=ExperimentHistory(root/'native.sqlite')
path=root/'native.json'
metadata={'experiment_id':'DG-native-001','device_id':'NATIVE-QA','experiment_type':'dual_gate_sweep','started_utc':'2026-10-26T14:05:10Z','status':'completed','metadata_path':str(path),'settings':{'loadable':{'center_nm':850}}}
path.write_text(json.dumps(metadata));history.upsert(metadata)
widget=QuickHistoryWidget(history,root,'NATIVE-QA')
widget.setAttribute(Qt.WidgetAttribute.WA_DontShowOnScreen,True)
widget.resize(1060,680);widget.show()
for _ in range(200):
 QTest.qWait(10)
 if widget.view_model.total:break
assert widget.view_model.total == 1
widget.view_model.selectRow(0)
for _ in range(200):
 QTest.qWait(10)
 if widget.view_model.canOpen:break
for theme in ('light','dark'):
 widget.set_theme(theme);QTest.qWait(80)
 image=widget.grabFramebuffer()
 assert not image.isNull()
 assert image.save(str(root/('native-'+theme+'.png')))
widget.resize(640,720);QTest.qWait(80)
assert widget.grabFramebuffer().save(str(root/'native-narrow.png'))
assert not widget.qml_warnings,widget.qml_warnings
widget.close();widget.deleteLater();QTest.qWait(20)
'''
    env = os.environ.copy()
    env["QT_QPA_PLATFORM"] = "windows"
    env["QT_QUICK_BACKEND"] = "software"
    result = subprocess.run([sys.executable, "-c", script, str(tmp_path)],
                            cwd=Path(__file__).resolve().parents[1], env=env,
                            capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stdout + result.stderr
    assert (tmp_path / "native-light.png").is_file()
