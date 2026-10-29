"""Live log previews stay bounded without discarding the retained document."""
import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest
from PySide6.QtTest import QSignalSpy, QTest
from PySide6.QtWidgets import QPlainTextEdit, QPushButton, QTextEdit

from tests.quick_helpers import quick_application, drain_qt_deletions
from tests.test_quick_parameter_forms import find_item
from ui import quick_parameter_forms as forms


def publish_logs(view):
    QTest.qWait(forms.LOG_PREVIEW_INTERVAL_MS + 60)


@pytest.fixture(params=[QTextEdit, QPlainTextEdit])
def log_view(request):
    quick_application()
    source = request.param()
    source.setReadOnly(True)
    # Parked source controls remain logically visible in the compatibility view.
    adapter = forms.WidgetsParameterAdapter([[('run_log', source, 'log')]], parking=source)
    view = forms.QuickParameterForm(adapter)
    warnings = []
    view.engine().warnings.connect(lambda errors: warnings.extend(str(error) for error in errors))
    view.resize(700, 210)
    view.show()
    QTest.qWait(20)
    field = next(iter(adapter._fields))
    yield source, view, field, warnings
    view.shutdown()
    view.close()
    view.deleteLater()
    source.deleteLater()
    drain_qt_deletions()


def test_long_log_reads_only_tail_and_keeps_complete_original(log_view, monkeypatch):
    source, view, field, warnings = log_view
    original = '\n'.join(f'Log entry {index}: acquired successfully' for index in range(30000))
    source.setPlainText(original)
    # Preview publication must never obtain the complete retained log string.
    monkeypatch.setattr(source, 'toPlainText', lambda: pytest.fail('full source read'))
    view.adapter.refresh_dirty()
    publish_logs(view)
    assert source.document().toPlainText() == original
    assert source.document().blockCount() == 30000
    assert field.text == '\n'.join(original.splitlines()[-forms.LOG_PREVIEW_BLOCKS:])
    assert len(field.text) <= forms.LOG_PREVIEW_CHARS
    assert field.logNotice.startswith('Showing latest log entries')
    rendered = find_item(view.rootObject(), 'logText_run_log')
    assert rendered.property('text') == field.text
    source.appendPlainText('LATEST ACQUISITION') if isinstance(source, QPlainTextEdit) else source.append('LATEST ACQUISITION')
    view.adapter.refresh_dirty()
    publish_logs(view)
    assert field.text.endswith('LATEST ACQUISITION')
    assert rendered.property('text').endswith('LATEST ACQUISITION')
    assert source.document().blockCount() == 30001
    assert not warnings


def test_long_single_line_has_bounded_copy_and_clear_replace_updates(log_view):
    source, view, field, _ = log_view
    source.setPlainText('x' * 200000 + 'FINAL')
    view.adapter.refresh_dirty()
    publish_logs(view)
    assert len(field.text) == forms.LOG_PREVIEW_CHARS
    assert field.text.endswith('FINAL') and field.logNotice
    source.clear()
    view.adapter.refresh_dirty()
    publish_logs(view)
    assert field.text == '' and field.logNotice == ''
    source.setPlainText('Replaced log\nLatest replacement')
    view.adapter.refresh_dirty()
    publish_logs(view)
    assert field.text == 'Replaced log\nLatest replacement' and field.logNotice == ''


def test_unchanged_and_hidden_log_do_no_document_copy_or_text_reset(log_view, monkeypatch):
    source, view, field, _ = log_view
    source.setPlainText('Beginning\nEnd')
    view.adapter.refresh_dirty()
    publish_logs(view)
    rendered = find_item(view.rootObject(), 'logText_run_log')
    rendered.setProperty('cursorPosition', 3)
    published = QSignalSpy(field.changed)
    reads = []
    read_tail = forms._log_tail
    monkeypatch.setattr(forms, '_log_tail', lambda document: (reads.append(document), read_tail(document))[1])
    for _ in range(5):
        view.adapter.refresh()
    assert reads == [] and published.count() == 0
    assert rendered.property('cursorPosition') == 3
    view.hide()
    source.setPlainText('Changed while hidden')
    QTest.qWait(10)
    # Even an explicit reconciliation of a hidden form skips log text reads.
    view.adapter.refresh()
    assert reads == []
    assert not view.adapter._timer.isActive() and not view._refresh.isActive()
    assert not view.adapter._log_timer.isActive()
    view.show()
    assert len(reads) == 1 and field.text == 'Changed while hidden'


def test_log_burst_coalesces_without_delaying_controls(monkeypatch):
    quick_application()
    source = QTextEdit()
    source.setReadOnly(True)
    button = QPushButton('Run')
    adapter = forms.WidgetsParameterAdapter(
        [[('run_log', source, 'log')], [('run', button, 'button')]], parking=source)
    view = forms.QuickParameterForm(adapter)
    view.resize(500, 230)
    view.show()
    QTest.qWait(20)
    fields = {widget: field for field, widget in adapter._fields.items()}
    reads = []
    read_tail = forms._log_tail
    monkeypatch.setattr(forms, '_log_tail', lambda document: (reads.append(document), read_tail(document))[1])
    try:
        published = QSignalSpy(fields[source].changed)
        for index in range(40):
            source.append(f'Burst entry {index}')
            # The normal fallback/dirty paths must not bypass log coalescing.
            adapter.refresh_dirty()
            adapter.refresh()
        assert adapter._log_timer.isActive() and reads == []
        button.setEnabled(False)
        QTest.qWait(30)
        assert not fields[button].enabled
        assert reads == [] and published.count() == 0
        publish_logs(view)
        assert len(reads) == 1 and published.count() == 1
        assert fields[source].text.endswith('Burst entry 39')
        source.append('Pending before hiding')
        assert adapter._log_timer.isActive()
        view.hide()
        publish_logs(view)
        assert len(reads) == 1 and not adapter._log_timer.isActive()
        view.show()
        assert len(reads) == 2 and fields[source].text.endswith('Pending before hiding')
        source.clear()
        publish_logs(view)
        assert fields[source].text == '' and len(reads) == 3
    finally:
        view.shutdown()
        view.close()
        view.deleteLater()
        source.deleteLater()
        button.deleteLater()
        drain_qt_deletions()


def test_full_log_opens_retained_live_document_and_rebind_releases_it(log_view):
    source, view, field, _ = log_view
    source.setPlainText('\n'.join(f'entry {index}' for index in range(30000)))
    view.adapter.refresh_dirty()
    assert field.openLog()
    dialog = view.adapter._log_dialogs[field]
    viewer = dialog.findChild(type(source), 'FullLogText')
    assert viewer.isReadOnly() and viewer.document() is source.document()
    assert viewer.document().blockCount() == 30000
    assert field.openLog() and view.adapter._log_dialogs[field] is dialog
    source.setPlainText('Live replacement')
    assert viewer.document().toPlainText() == 'Live replacement'
    replacement = type(source)()
    replacement.setReadOnly(True)
    replacement.setPlainText('Replacement source')
    try:
        view.adapter.rebind_source(field, replacement)
        assert not dialog.isVisible()
        assert field not in view.adapter._log_dialogs
        assert field.text == 'Replacement source'
        drain_qt_deletions()
        assert source.document().toPlainText() == 'Live replacement'
    finally:
        replacement.deleteLater()
