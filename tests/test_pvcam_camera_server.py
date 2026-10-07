import hashlib
import json
import struct
import threading
from pathlib import Path

import pytest

from tools.winspec import pvcam_camera_server as server
from tools.winspec.pvcam_startup_probe import RecoveryRequired


class SDK:
    def __init__(self):
        self.unsafe = False
        self.active = False
        self.completed = False
        self.temperature = -100.
        self.events = []
        self.original = dict(ser_size=512, par_size=1, bit_depth=16, exp_res=0,
                             exposure_mode=0, temp_setpoint=-10000, gain_index=2,
                             spdtab_index=0, clear_cycles=1, clear_mode=2, shutter_mode=1)
        self.fail_status = self.fail_abort = self.fail_finish = False
        self.partial = False
        self.status_hook = None
        self.elapsed = 0.
        self.completion_s = None

    def open(self): self.events.append('open')
    def snapshot(self):
        assert not self.active
        return dict(self.original)
    def cold(self):
        assert not self.active
        if self.temperature > -100: raise RuntimeError('Hot camera')
        return self.temperature
    def setup(self, frames, exposure):
        assert not self.active
        self.events.append(('setup', frames, exposure))
        self.frames = frames
        self.exposure = exposure
        self.size = frames * 1024
        return self.size
    def pin(self, size): self.events.append(('pin', size))
    def release_buffer(self):
        assert not self.active and not self.unsafe
        self.events.append('release_buffer')
    def start(self):
        self.events.append('start')
        self.active = True
        self.completed = False
    def status(self):
        if self.status_hook: self.status_hook()
        if self.fail_status: raise RuntimeError('Status failed')
        self.active = False
        self.completed = not self.partial
        self.elapsed += self.completion_s if self.completion_s is not None else self.frames*self.exposure/1000.
        return 3, self.size - (2 if self.partial else 0)
    def raw(self):
        return b''.join(struct.pack('<512H', *([65000 + i] * 512)) for i in range(self.frames))
    def abort(self):
        self.events.append('abort')
        if self.fail_abort:
            self.unsafe = True
            raise RecoveryRequired('Abort failed')
        self.active = False
        self.completed = False
    def finish(self):
        assert self.completed, 'finish_seq requires completed readout, not just setup'
        self.events.append('finish')
        if self.fail_finish:
            self.unsafe = True
            raise RecoveryRequired('Finish failed')
    def close(self): self.events.append('close')


def bridge(tmp_path, **options):
    sdk = SDK()
    b = server.PVCAMBridge(sdk, str(tmp_path),
                          dict(exposure_ms=800, accumulations=1, sequential_frames=1,
                               temperature_setpoint_c=-100.), dict(sdk.original), **options)
    b.initialize()
    b.execute('SET_SETTINGS', dict(exposure_ms=500, accumulations=2, sequential_frames=1))
    b.timing = lambda: dict(qpc_s=sdk.elapsed, unix_s=1790890000.+sdk.elapsed,
                            tick_ms=(0xfffffff0+int(sdk.elapsed*1000)) & 0xffffffff)
    return b, sdk


def capture(b):
    settings, _ = b.execute('GET_SETTINGS', {})
    return b.execute('ACQUIRE_GUARDED', dict(settings_mode='managed',
                                           expected_settings=settings['settings']))


def test_finish_each_diagnostic_archives_before_finish_and_reuses_setup(tmp_path):
    b, sdk = bridge(tmp_path, diagnostic_finish_each=True)
    native_finish = sdk.finish
    archived = []
    def finish():
        files = sorted(tmp_path.glob('capture-*.cleanup-pending.json'))
        pending = [json.loads(p.read_text()) for p in files if json.loads(p.read_text())['raw_archive'] not in archived]
        assert len(pending) == 1
        assert Path(pending[0]['raw_archive']).exists()
        archived.append(pending[0]['raw_archive'])
        native_finish()
    sdk.finish = finish
    for _ in range(3):
        meta, payload = capture(b)
        assert meta['server_build'] == '2026-10-01-pvcam-backend-v2-finish-each'
        assert meta['diagnostic_sequence_cleanup']['completed'] is True
        assert struct.unpack('<512d', payload) == (130001.,)*512
        assert json.loads(Path(meta['raw_archive']).with_suffix('.json').read_text()) == meta
        assert b.complete_transfer(True)
    assert len(archived) == 3
    assert sdk.events.count(('setup', 2, 500)) == 1
    assert sdk.events.count('finish') == 3
    b.execute('SET_SETTINGS', dict(exposure_ms=501, accumulations=2, sequential_frames=1))
    b.close()
    assert sdk.events.count('finish') == 3


def test_finish_each_failure_preserves_raw_blocks_delivery_and_retains_resources(tmp_path):
    b, sdk = bridge(tmp_path, diagnostic_finish_each=True)
    sdk.fail_finish = True
    with pytest.raises(RecoveryRequired, match='Finish failed'):
        capture(b)
    saved = json.loads(next(p for p in tmp_path.glob('capture-*.json') if not p.name.endswith('.cleanup-pending.json')).read_text())
    assert Path(saved['raw_archive']).exists()
    assert saved['ok'] is False and saved['native_health'] == 'recovery_required'
    assert saved['diagnostic_sequence_cleanup']['completed'] is False
    assert saved['diagnostic_sequence_cleanup']['native_finish_called'] is True
    assert not b.pending_transfer
    events = list(sdk.events)
    with pytest.raises(RecoveryRequired): b.close()
    with pytest.raises(RecoveryRequired): capture(b)
    assert sdk.events == events


@pytest.mark.parametrize('mode', ['cancel', 'partial', 'duration'])
def test_finish_each_never_finishes_cancelled_partial_or_rejected_capture(tmp_path, mode):
    b, sdk = bridge(tmp_path, diagnostic_finish_each=True)
    if mode == 'cancel': sdk.status_hook = lambda: b.execute('STOP', {})
    if mode == 'partial': sdk.partial = True
    if mode == 'duration': sdk.completion_s = .5
    with pytest.raises(RuntimeError): capture(b)
    assert 'finish' not in sdk.events
    assert not b.pending_transfer
    if mode == 'duration':
        b.close()
        assert sdk.events.count('finish') == 1


def test_finish_each_pending_receipt_still_blocks_next_start(tmp_path):
    b, sdk = bridge(tmp_path, diagnostic_finish_each=True)
    capture(b)
    with pytest.raises(RuntimeError, match='transfer'): capture(b)
    assert sdk.events.count('start') == 1 and sdk.events.count('finish') == 1


def test_finish_each_does_not_finish_a_never_started_sequence(tmp_path):
    b, sdk = bridge(tmp_path, diagnostic_finish_each=True)
    b.close()
    assert 'finish' not in sdk.events


def test_archive_placebo_matches_write_order_but_keeps_completed_readout(tmp_path, monkeypatch):
    b, sdk = bridge(tmp_path, diagnostic_archive_placebo=True)
    writes = []
    original_write = server.write_raw
    def record(path, data):
        original_write(path, data)
        writes.append(Path(path).name)
    monkeypatch.setattr(server, 'write_raw', record)
    sdk.fail_finish = True  # A native finish would make this control fail.
    for _ in range(3):
        metadata, payload = capture(b)
        assert metadata['server_build'] == '2026-10-01-pvcam-backend-v2-archive-placebo'
        cleanup = metadata['diagnostic_sequence_cleanup']
        assert cleanup['completed'] is True and cleanup['native_finish_called'] is False
        assert b.readout_completed is True
        assert struct.unpack('<512d', payload) == (130001.,)*512
        assert writes[-3].endswith('.raw')
        assert writes[-2].endswith('.cleanup-pending.json')
        assert writes[-1].endswith('.json') and not writes[-1].endswith('.cleanup-pending.json')
        assert json.loads(Path(metadata['raw_archive']).with_suffix('.json').read_text()) == metadata
        b.complete_transfer(True)
    assert sdk.events.count('finish') == 0
    assert sdk.events.count(('setup', 2, 500)) == 1
    sdk.fail_finish = False
    b.execute('SET_SETTINGS', dict(exposure_ms=501, accumulations=2, sequential_frames=1))
    assert sdk.events.count('finish') == 1
    b.close()
    assert sdk.events.count('finish') == 1


def test_archive_placebo_keeps_receipt_guard_and_finishes_on_ordinary_close(tmp_path):
    b, sdk = bridge(tmp_path, diagnostic_archive_placebo=True)
    capture(b)
    with pytest.raises(RuntimeError, match='transfer'): capture(b)
    assert sdk.events.count('start') == 1 and sdk.events.count('finish') == 0
    b.complete_transfer(True)
    b.close()
    assert sdk.events.count('finish') == 1


def test_diagnostic_cleanup_modes_are_mutually_exclusive(tmp_path):
    with pytest.raises(ValueError, match='exclusive'):
        bridge(tmp_path, diagnostic_finish_each=True, diagnostic_archive_placebo=True)


@pytest.mark.parametrize('mode', ['cancel', 'partial', 'duration'])
def test_archive_placebo_preserves_rejection_without_extra_cleanup(tmp_path, mode):
    b, sdk = bridge(tmp_path, diagnostic_archive_placebo=True)
    if mode == 'cancel': sdk.status_hook = lambda: b.execute('STOP', {})
    if mode == 'partial': sdk.partial = True
    if mode == 'duration': sdk.completion_s = .5
    with pytest.raises(RuntimeError): capture(b)
    assert 'finish' not in sdk.events
    assert not list(tmp_path.glob('*.cleanup-pending.json'))
    assert not b.pending_transfer


def test_repeated_capture_sums_without_uint16_overflow_and_reuses_setup(tmp_path):
    b, sdk = bridge(tmp_path)
    for _ in range(3):
        m, payload = capture(b)
        assert struct.unpack('<512d', payload) == (130001.,) * 512
        assert m['acquisition_backend'] == 'pvcam'
        assert m['temperature_guard']['sample_count'] == 2
        raw = Path(m['raw_archive']).read_bytes()
        assert hashlib.sha256(raw).hexdigest() == m['raw_sha256']
        b.complete_transfer(True)
    assert sdk.events.count(('setup', 2, 500)) == 1
    assert sdk.events.count('start') == 3


def test_pending_or_failed_transfer_blocks_buffer_reuse(tmp_path):
    b, sdk = bridge(tmp_path)
    m, _ = capture(b)
    with pytest.raises(RuntimeError, match='transfer'): capture(b)
    b.complete_transfer(False)
    with pytest.raises(RuntimeError): capture(b)
    assert Path(m['raw_archive']).exists() and sdk.events.count('start') == 1


@pytest.mark.parametrize('field', ['temperature', 'partial', 'fail_status'])
def test_hot_partial_and_failed_captures_never_produce_success(tmp_path, field):
    b, sdk = bridge(tmp_path)
    setattr(sdk, field, -99. if field == 'temperature' else True)
    with pytest.raises(RuntimeError): capture(b)
    assert not b.pending_transfer
    assert not list(tmp_path.glob('capture-*.json'))


def test_changed_native_gain_invalidates_acquisition_before_start(tmp_path):
    b, sdk = bridge(tmp_path)
    sdk.original['gain_index'] = 1
    with pytest.raises(RuntimeError, match='changed'): capture(b)
    assert 'start' not in sdk.events


def test_stop_signal_is_processed_without_native_calls_on_request_thread(tmp_path):
    b, sdk = bridge(tmp_path)
    def stop():
        reply, _ = b.execute('STOP', {})
        assert reply['stop_requested']
    sdk.status_hook = stop
    with pytest.raises(RuntimeError, match='stopped'): capture(b)
    assert 'abort' in sdk.events
    assert not b.pending_transfer
    b.execute('SET_SETTINGS', dict(exposure_ms=500, accumulations=2, sequential_frames=1))
    sdk.status_hook = None
    m, _ = capture(b)
    b.complete_transfer(True)
    assert m['settings']['accumulations'] == 2


def test_failed_abort_never_releases_or_restores_native_resources(tmp_path):
    b, sdk = bridge(tmp_path)
    sdk.fail_status = sdk.fail_abort = True
    with pytest.raises(RecoveryRequired): capture(b)
    assert sdk.unsafe
    before = list(sdk.events)
    with pytest.raises(RecoveryRequired): b.close()
    assert sdk.events == before


def test_shutdown_finishes_restores_recipe_and_closes_without_cooling_write(tmp_path):
    b, sdk = bridge(tmp_path)
    capture(b)
    b.complete_transfer(True)
    b.close()
    last_setup = sdk.events.index(('setup', 1, 800), sdk.events.index('start'))
    assert sdk.events.index('finish', sdk.events.index('start')) < last_setup < sdk.events.index('close')


@pytest.mark.parametrize('settings', [dict(exposure_ms=.5), dict(exposure_ms=True),
    dict(accumulations=0), dict(accumulations=1.5), dict(accumulations=65),
    dict(sequential_frames=2), dict(controller_gain=1), dict(exposure_ms=float('nan'))])
def test_invalid_requests_do_not_change_recipe_or_prepare(tmp_path, settings):
    b, sdk = bridge(tmp_path)
    before = list(sdk.events)
    with pytest.raises((ValueError, RuntimeError)): b.execute('SET_SETTINGS', settings)
    assert sdk.events == before


def test_expected_settings_mismatch_does_not_start(tmp_path):
    b, sdk = bridge(tmp_path)
    with pytest.raises(RuntimeError, match='expected'):
        b.execute('ACQUIRE_GUARDED', dict(settings_mode='managed', expected_settings={'exposure_ms':501}))
    assert 'start' not in sdk.events


def test_never_started_and_aborted_sequences_are_not_finished_as_complete(tmp_path):
    b, sdk = bridge(tmp_path)
    assert 'finish' not in sdk.events
    sdk.status_hook = lambda: b.execute('STOP', {})
    with pytest.raises(RuntimeError): capture(b)
    b.execute('SET_SETTINGS', dict(exposure_ms=501, accumulations=2, sequential_frames=1))
    assert 'finish' not in sdk.events
    b.close()
    assert 'finish' not in sdk.events


def test_close_signals_active_capture_before_waiting_for_ownership(tmp_path):
    b, sdk = bridge(tmp_path)
    entered=threading.Event();closed=threading.Event();errors=[]
    def status():
        entered.set()
        b.stop.wait(2)
        return 2,0
    sdk.status=status
    def run():
        try: capture(b)
        except RuntimeError as error: errors.append(str(error))
    worker=threading.Thread(target=run);worker.start()
    assert entered.wait(1)
    def close():
        b.close();closed.set()
    closer=threading.Thread(target=close);closer.start()
    ended=closed.wait(.5)
    if not ended: b.execute('STOP', {})
    worker.join(3);closer.join(3)
    assert ended and errors and 'abort' in sdk.events


@pytest.mark.parametrize('failure',['geometry','gain','temperature'])
def test_initialization_rejection_closes_without_configuring_detector(tmp_path,failure):
    sdk=SDK();expected=dict(sdk.original)
    if failure=='geometry': sdk.original['ser_size']=1024
    if failure=='gain': sdk.original['gain_index']=1
    if failure=='temperature': sdk.temperature=-99.
    b=server.PVCAMBridge(sdk,str(tmp_path),dict(exposure_ms=800,accumulations=1,
        sequential_frames=1,temperature_setpoint_c=-100.),expected)
    with pytest.raises(RuntimeError):b.initialize()
    b.close()
    assert not any(isinstance(event,tuple) and event[0]=='setup' for event in sdk.events)
    assert sdk.events[-1]=='close'


def test_shutdown_cannot_be_rearmed_by_configuration_already_in_progress(tmp_path):
    b,sdk=bridge(tmp_path);entered=threading.Event();release=threading.Event()
    cold=sdk.cold
    def block_once():
        entered.set();release.wait(2);sdk.cold=cold
        return cold()
    sdk.cold=block_once
    configure=threading.Thread(target=lambda:b.execute('SET_SETTINGS',dict(exposure_ms=501)))
    configure.start();assert entered.wait(1)
    closer=threading.Thread(target=b.close);closer.start()
    assert b.stop.wait(1)
    release.set();configure.join(3);closer.join(3)
    assert b.stop.is_set()
    with pytest.raises(RuntimeError):capture(b)
    assert 'start' not in sdk.events


def test_short_completed_sequence_retains_evidence_and_prohibits_reuse(tmp_path):
    b, sdk = bridge(tmp_path)
    sdk.completion_s = .5  # Full bytes arrive after only half the requested 1 s.
    with pytest.raises(RuntimeError, match='duration'):
        capture(b)
    raw_files = list(tmp_path.glob('capture-*.raw'))
    assert len(raw_files) == 1
    metadata = json.loads(raw_files[0].with_suffix('.json').read_text())
    assert metadata['ok'] is False
    assert metadata['minimum_duration_guard']['passed'] is False
    assert metadata['sequence_timing']['elapsed_qpc_s'] == pytest.approx(.5)
    assert metadata['raw_sha256'] == hashlib.sha256(raw_files[0].read_bytes()).hexdigest()
    assert not b.pending_transfer and not sdk.unsafe
    with pytest.raises(RuntimeError): b.execute('SET_SETTINGS', dict(exposure_ms=500))
    assert sdk.events.count('start') == 1 and 'abort' not in sdk.events
    b.close()  # Completed readout can be finished/restored normally.
    assert sdk.events[-1] == 'close'


def test_sequence_timing_records_complete_status_and_tick_wrap(tmp_path):
    b, sdk = bridge(tmp_path)
    metadata, _ = capture(b)
    timing = metadata['sequence_timing']
    assert timing['elapsed_qpc_s'] == pytest.approx(1.)
    assert timing['elapsed_tick_s'] == pytest.approx(1.)
    assert timing['start']['tick_ms'] == 0xfffffff0
    assert timing['complete']['tick_ms'] == 984
    assert timing['status_trace'][-1]['status'] == 3
    assert timing['status_trace'][-1]['bytes_arrived'] == 2048
    assert metadata['minimum_duration_guard']['passed'] is True
    b.complete_transfer(True)
