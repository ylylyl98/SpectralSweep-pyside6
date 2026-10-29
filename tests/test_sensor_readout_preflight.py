from copy import deepcopy
import threading

import pytest
from PySide6.QtCore import QObject, QTimer, Signal
from PySide6.QtWidgets import QApplication, QLabel, QMessageBox, QVBoxLayout, QWidget

from tests.quick_helpers import quick_application, drain_qt_deletions
from ui import sensor_readout_preflight as preflight


def snapshot(selection='CustomRegions', *, height=8, y_binning=8, fingerprint='custom'):
    return {
        'selection': selection,
        'regions': [{'x': 12, 'y': 4, 'width': 100, 'height': height,
                     'x_binning': 2, 'y_binning': y_binning}],
        'full_regions': [{'x': 0, 'y': 0, 'width': 1024, 'height': 256,
                          'x_binning': 1, 'y_binning': 1}],
        'sensor_width': 1024, 'sensor_height': 256,
        'provider': 'Hardware', 'experiment': 'experiment A',
        'identity': {'serial': 'camera A'}, 'session_id': 'session A',
        'fingerprint': fingerprint,
    }


class Controller(QObject):
    sensor_preflight_finished = Signal(object)

    def __init__(self, state=None):
        super().__init__()
        self.snapshots = {'lightfield': state or snapshot()}
        self.requests = []
        self.error = None
        self.hold = False
        self.stale = None
        self.background = False
        self.pending_flags = []
        self.parent_page = None
        self.threads = []

    def request_sensor_preflight(self, request):
        self.requests.append(request)
        if self.parent_page is not None:
            self.pending_flags.append(self.parent_page._sensor_preflight_pending)
        if self.hold:
            return
        if self.stale is not None:
            self.sensor_preflight_finished.emit(self.stale)
            self.stale = None

        def finish():
            if self.error and request['action'] == 'apply':
                request['error'] = self.error
            else:
                if request['action'] == 'apply':
                    for backend, intent in request['intents'].items():
                        if intent['mode'] == 'custom':
                            continue
                        state = self.snapshots[backend]
                        state['selection'] = 'BinAll' if intent['mode'] == 'bin_all' else 'FullSensor'
                        state['regions'] = deepcopy(state['full_regions'])
                        if intent['mode'] == 'bin_all':
                            state['regions'][0]['y_binning'] = state['sensor_height']
                        state['fingerprint'] = state['selection']
                request['snapshots'] = deepcopy(self.snapshots)
            self.sensor_preflight_finished.emit(request)

        if self.background:
            thread = threading.Thread(target=finish)
            self.threads.append(thread)
            thread.start()
        else:
            QTimer.singleShot(1, finish)


@pytest.fixture
def page(monkeypatch):
    quick_application()
    widget = QWidget()
    QVBoxLayout(widget)
    errors = []
    monkeypatch.setattr(QMessageBox, 'critical', lambda *args: errors.append(args[-1]))
    widget.preflight_errors = errors
    yield widget
    widget.close()
    widget.deleteLater()
    drain_qt_deletions()


def test_bin_all_is_applied_without_prompt_even_for_full_sensor(page, monkeypatch):
    ctrl = Controller(snapshot('FullSensor', height=256, y_binning=1))
    ctrl.parent_page = page
    monkeypatch.setattr(preflight, '_confirm_custom', lambda *_: pytest.fail('unexpected prompt'))
    assert preflight.ensure_sensor_readout(page, ctrl, backends=['lightfield'])
    assert [r['action'] for r in ctrl.requests] == ['read', 'apply']
    applied = ctrl.requests[1]
    assert applied['intents']['lightfield'] == {
        'mode': 'bin_all', 'expected_fingerprint': 'custom'}
    assert applied['backends'] == ['lightfield']
    assert applied['session_id'] == 'session A'
    assert ctrl.snapshots['lightfield']['regions'][0]['y_binning'] == 256
    assert ctrl.pending_flags == [True, True]
    assert page._sensor_preflight_pending is False
    assert '1024 × 1' in page._sensor_readout_summary.text()


def test_2d_custom_uses_full_sensor_without_prompt(page, monkeypatch):
    ctrl = Controller()
    monkeypatch.setattr(preflight, '_confirm_custom', lambda *_: pytest.fail('unexpected prompt'))
    assert preflight.ensure_sensor_readout(page, ctrl, mode='2d')
    assert ctrl.requests[1]['intents']['lightfield']['mode'] == 'full_sensor'
    assert '1024 × 256' in page._sensor_readout_summary.text()


def test_keep_reuses_confirmation_across_pages_and_changes_prompt_again(page, monkeypatch):
    ctrl = Controller()
    prompts = []
    monkeypatch.setattr(preflight, '_confirm_custom', lambda _, state: prompts.append(state) or 'custom')
    assert preflight.ensure_sensor_readout(page, ctrl)
    assert ctrl.requests[1]['intents']['lightfield']['snapshot']['fingerprint'] == 'custom'
    other = QWidget()
    QVBoxLayout(other)
    try:
        assert preflight.ensure_sensor_readout(other, ctrl)
        assert len(prompts) == 1
        for key, value in [('fingerprint', 'changed ROI'), ('session_id', 'reconnected'),
                           ('experiment', 'experiment B'), ('identity', {'serial': 'camera B'})]:
            ctrl.snapshots['lightfield'][key] = value
            assert preflight.ensure_sensor_readout(page, ctrl)
        assert len(prompts) == 5
    finally:
        other.deleteLater()


@pytest.mark.parametrize('regions', [
    [snapshot(height=8, y_binning=1)['regions'][0]],
    snapshot()['regions'] * 2,
    [],
    [dict(snapshot()['regions'][0], y_binning=3)],
])
def test_incompatible_custom_disables_keep_and_explains_shape(page, monkeypatch, regions):
    state = snapshot()
    state['regions'] = regions
    observed = {}

    def choose(box):
        observed['message'] = box.informativeText()
        observed['buttons'] = {button.text(): button.isEnabled() for button in box.buttons()}
        next(button for button in box.buttons() if button.text() == 'Switch to Bin All').click()
        return 0

    monkeypatch.setattr(QMessageBox, 'exec', choose)
    assert preflight._confirm_custom(page, state) == 'bin_all'
    assert observed['buttons']['Keep current Custom Region'] is False
    assert 'exactly one ROI with output height 1' in observed['message']
    assert 'cannot be flattened' in observed['message']


def test_custom_prompt_shows_bounds_binning_and_output(page, monkeypatch):
    observed = {}

    def choose(box):
        observed['message'] = box.informativeText()
        keep = next(button for button in box.buttons() if button.text() == 'Keep current Custom Region')
        observed['enabled'] = keep.isEnabled()
        keep.click()
        return 0

    monkeypatch.setattr(QMessageBox, 'exec', choose)
    assert preflight._confirm_custom(page, snapshot()) == 'custom'
    assert observed['enabled']
    assert 'x=12…111, y=4…11; binning 2 × 8; output 50 × 1' in observed['message']


def test_prompt_cancel_does_not_apply_or_cache(page, monkeypatch):
    ctrl = Controller()
    monkeypatch.setattr(preflight, '_confirm_custom', lambda *_: None)
    assert not preflight.ensure_sensor_readout(page, ctrl)
    assert [r['action'] for r in ctrl.requests] == ['read']
    assert ctrl._sensor_readout_confirmed_keep is None
    assert page._sensor_preflight_pending is False


def test_failed_apply_is_visible_and_does_not_cache(page, monkeypatch):
    ctrl = Controller()
    ctrl.error = 'Readback disagrees with requested ROI'
    prompts = []
    monkeypatch.setattr(preflight, '_confirm_custom', lambda *_: prompts.append(True) or 'custom')
    assert not preflight.ensure_sensor_readout(page, ctrl)
    assert page.preflight_errors == [ctrl.error]
    assert ctrl.error in page._sensor_readout_summary.text()
    assert ctrl._sensor_readout_confirmed_keep is None
    ctrl.error = None
    assert preflight.ensure_sensor_readout(page, ctrl)
    assert len(prompts) == 2


def test_cancelled_async_request_ignores_stale_completion(page, monkeypatch):
    ctrl = Controller(snapshot('BinAll'))
    ctrl.hold = True

    def cancel_dialog():
        for widget in QApplication.topLevelWidgets():
            if isinstance(widget, preflight._RequestDialog):
                widget.reject()

    QTimer.singleShot(5, cancel_dialog)
    assert not preflight.ensure_sensor_readout(page, ctrl)
    old_request = ctrl.requests[0]
    assert old_request['cancel_event'].is_set()
    assert page._sensor_preflight_pending is False
    ctrl.hold = False
    ctrl.stale = old_request
    monkeypatch.setattr(preflight, '_confirm_custom', lambda *_: pytest.fail('unexpected prompt'))
    assert preflight.ensure_sensor_readout(page, ctrl)
    assert [r['action'] for r in ctrl.requests] == ['read', 'read', 'apply']


def test_worker_thread_completion_runs_in_responsive_modal_loop(page):
    ctrl = Controller(snapshot('BinAll'))
    ctrl.background = True
    assert preflight.ensure_sensor_readout(page, ctrl)
    for thread in ctrl.threads:
        thread.join(timeout=1)
        assert not thread.is_alive()
    assert page._sensor_preflight_pending is False


def test_fast_request_finishes_without_showing_progress(page, monkeypatch):
    shown = []
    monkeypatch.setattr(preflight._RequestDialog, 'show', lambda self: shown.append(self))
    assert preflight.ensure_sensor_readout(page, Controller(snapshot('LineSensor')))
    assert shown == []


def test_slow_request_shows_modal_and_remains_responsive_to_cancel(page, monkeypatch):
    ctrl = Controller(snapshot('LineSensor'))
    ctrl.hold = True
    shown, ticks = [], []
    original_show = preflight._RequestDialog.show

    def show(dialog):
        shown.append(dialog)
        original_show(dialog)
        QTimer.singleShot(5, dialog.reject)

    timer = QTimer()
    timer.setInterval(10)
    timer.timeout.connect(lambda: ticks.append(True))
    monkeypatch.setattr(preflight._RequestDialog, 'show', show)
    timer.start()
    try:
        assert not preflight.ensure_sensor_readout(page, ctrl)
    finally:
        timer.stop()
    assert len(shown) == 1
    assert ticks
    assert shown[0].isModal()
    assert ctrl.requests[0]['cancel_event'].is_set()
    assert page._sensor_preflight_pending is False


@pytest.mark.parametrize('cancel_after_ms', [2, 250])
def test_external_cancellation_closes_hidden_or_visible_wait_without_applying(page, monkeypatch, cancel_after_ms):
    ctrl = Controller(snapshot('LineSensor'))
    ctrl.hold = True
    shown = []
    original_show = preflight._RequestDialog.show

    def show(dialog):
        shown.append(True)
        original_show(dialog)

    def cancel():
        page._sensor_preflight_cancelled = True
        for request in ctrl.requests:
            request['cancel_event'].set()

    dispatch = ctrl.request_sensor_preflight
    def dispatch_then_cancel(request):
        dispatch(request)
        # Exercise cancellation of an already queued request. Scheduling this
        # before dialog construction can legitimately cancel before dispatch.
        QTimer.singleShot(cancel_after_ms, cancel)

    monkeypatch.setattr(preflight._RequestDialog, 'show', show)
    monkeypatch.setattr(ctrl, 'request_sensor_preflight', dispatch_then_cancel)
    assert not preflight.ensure_sensor_readout(page, ctrl)
    assert [r['action'] for r in ctrl.requests] == ['read']
    assert ctrl.requests[0]['cancel_event'].is_set()
    assert bool(shown) == (cancel_after_ms > 200)
    assert not page._sensor_preflight_pending


def test_cancelled_matching_result_rejects_instead_of_leaving_wait_open(page):
    class CancelledController(Controller):
        def request_sensor_preflight(self, request):
            self.requests.append(request)
            request['cancel_event'].set()
            self.sensor_preflight_finished.emit(request)

    ctrl = CancelledController()
    assert not preflight.ensure_sensor_readout(page, ctrl)
    assert len(ctrl.requests) == 1
    assert not page._sensor_preflight_pending


def test_cancellation_during_custom_choice_prevents_apply(page, monkeypatch):
    ctrl = Controller()

    def cancel_during_choice(parent, state):
        parent._sensor_preflight_cancelled = True
        return 'custom'

    monkeypatch.setattr(preflight, '_confirm_custom', cancel_during_choice)
    assert not preflight.ensure_sensor_readout(page, ctrl)
    assert [r['action'] for r in ctrl.requests] == ['read']
    assert ctrl._sensor_readout_confirmed_keep is None


def test_external_cancel_closes_open_custom_prompt(page):
    QTimer.singleShot(2, lambda: setattr(page, '_sensor_preflight_cancelled', True))
    assert preflight._confirm_custom(page, snapshot()) is None


def test_cancellation_during_apply_prevents_success_and_caching(page, monkeypatch):
    ctrl = Controller()
    original = ctrl.request_sensor_preflight

    def request(request):
        if request['action'] == 'apply':
            page._sensor_preflight_cancelled = True
        original(request)

    ctrl.request_sensor_preflight = request
    monkeypatch.setattr(preflight, '_confirm_custom', lambda *_: 'custom')
    assert not preflight.ensure_sensor_readout(page, ctrl)
    assert [r['action'] for r in ctrl.requests] == ['read', 'apply']
    assert ctrl._sensor_readout_confirmed_keep is None


def test_summary_uses_human_mode_provider_and_row_geometry():
    summary = preflight.format_sensor_summary(snapshot('LineSensor', height=256, y_binning=256))
    assert summary == 'Bin All · Hardware · rows 256 → 1 · output 50 × 1'


def test_nested_preflight_is_rejected_without_clearing_outer_pending(page):
    page._sensor_preflight_pending = True
    ctrl = Controller()
    assert not preflight.ensure_sensor_readout(page, ctrl)
    assert page._sensor_preflight_pending is True
    assert not ctrl.requests


def test_legacy_controller_and_empty_real_route_remain_supported(page):
    assert preflight.ensure_sensor_readout(page, object())
    assert page._sensor_preflight_pending is False
    ctrl = Controller()
    ctrl.snapshots = {}
    assert preflight.ensure_sensor_readout(page, ctrl)
    assert [r['action'] for r in ctrl.requests] == ['read']
    assert page._sensor_readout_summary.isHidden()


def test_existing_spectrum_hint_is_reused(page):
    page._sensor_mode_hint = QLabel(page)
    page.layout().addWidget(page._sensor_mode_hint)
    count = page.layout().count()
    assert preflight.ensure_sensor_readout(page, Controller(snapshot('BinAll')))
    assert page._sensor_readout_summary is page._sensor_mode_hint
    assert page.layout().count() == count
