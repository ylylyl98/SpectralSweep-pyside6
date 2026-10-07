import importlib.util
from pathlib import Path

import pytest

from tests.test_winspec_display_probe import display_bridge


def load_probe():
    path = Path('tools/winspec/startup_setparam_probe.py')
    assert path.exists(), 'SetParam probe has not been implemented'
    spec = importlib.util.spec_from_file_location('tools.winspec.startup_setparam_probe', path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def parameter_bridge(tmp_path):
    br, settings, display, events, docs, calls = display_bridge(tmp_path)
    exp = br.create_experiment()
    flags = {'EXP_AUTOSAVE': -1, 'EXP_BSHOWWINDOW': -1, 'EXP_NEWWINDOW': 1}
    br.get_param = lambda e, key: flags[key]
    def setter(key, value):
        events.append(('write', key, value))
        flags[key] = value
        return 0
    exp.SetParam = setter
    acquire = br.acquire
    def capture(e, **kwargs):
        autosave, visible = flags['EXP_AUTOSAVE'], flags['EXP_BSHOWWINDOW']
        e.SetParam('EXP_AUTOSAVE', False)
        if visible:
            e.SetParam('EXP_BSHOWWINDOW', 0)
        result = acquire(e, **kwargs)
        if visible:
            e.SetParam('EXP_BSHOWWINDOW', visible)
        e.SetParam('EXP_AUTOSAVE', autosave)
        return result
    br.acquire = capture
    return br, exp, settings, flags, events, docs, calls


def test_only_verified_unchanged_autosave_writes_are_skipped(tmp_path):
    probe = load_probe()
    br, exp, _, flags, events, _, _ = parameter_bridge(tmp_path)
    wrapped = probe.ParameterExperiment(br, exp, skip_autosave=True)
    wrapped.SetParam('EXP_AUTOSAVE', False)  # Changed value must reach COM.
    wrapped.SetParam('EXP_AUTOSAVE', False)
    wrapped.SetParam('EXP_BSHOWWINDOW', 0)
    wrapped.SetParam('EXP_BSHOWWINDOW', 0)
    assert [r['skipped'] for r in wrapped.writes] == [False, True, False, False]
    assert len(events) == 3 and flags['EXP_AUTOSAVE'] is False
    br.get_param = lambda *a: None
    with pytest.raises(RuntimeError, match='Boolean'):
        wrapped.SetParam('EXP_AUTOSAVE', False)
    assert len(events) == 3


def test_independent_arms_warmups_archives_and_original_state_restored(tmp_path):
    probe = load_probe()
    br, _, settings, flags, events, docs, calls = parameter_bridge(tmp_path)
    report = probe.run_probe(br, str(tmp_path), lambda r: None, pairs=2, measured=1)
    assert report['status'] == 'complete' and report['restore_ok']
    assert flags == {'EXP_AUTOSAVE': -1, 'EXP_BSHOWWINDOW': -1, 'EXP_NEWWINDOW': 1}
    assert settings['exposure_ms'] == 800. and settings['accumulations'] == 1
    assert len(report['frames']) == 16 and len(calls) == 16
    assert len({id(d) for d in calls}) == 16 and all(d.closed for d in docs)
    assert len(list(tmp_path.glob('archive-*.spe'))) == 16
    assert len(report['summary']['autosave']['paired_start_saved_s']) == 2
    assert len(report['summary']['display']['paired_start_saved_s']) == 2
    for frame in report['frames']:
        writes = frame['parameter_writes']
        auto = [w for w in writes if w['key'] == 'EXP_AUTOSAVE']
        show = [w for w in writes if w['key'] == 'EXP_BSHOWWINDOW']
        assert len(auto) == 2
        assert all(w['skipped'] == (frame['experiment'] == 'autosave' and frame['arm'] == 'optimized') for w in auto)
        assert len(show) == (0 if frame['experiment'] == 'display' and frame['arm'] == 'optimized' else 2)
        assert not any(w['skipped'] for w in show)


def test_busy_or_unknown_state_blocks_configuration(tmp_path):
    probe = load_probe()
    for field, value in [('controller_running', True), ('running', None)]:
        br, _, settings, _, events, _, calls = parameter_bridge(tmp_path)
        settings[field] = value
        with pytest.raises(RuntimeError, match='idle'):
            probe.run_probe(br, str(tmp_path), lambda r: None, pairs=1, measured=1)
        assert not events and not calls


def test_archive_failure_retains_unacknowledged_frame_and_restores(tmp_path, monkeypatch):
    probe = load_probe()
    br, _, settings, flags, _, docs, calls = parameter_bridge(tmp_path)
    monkeypatch.setattr(probe, 'archive_spe', lambda *a: (_ for _ in ()).throw(OSError('disk full')))
    report = probe.run_probe(br, str(tmp_path), lambda r: None, pairs=1, measured=1)
    assert report['status'] == 'failed' and report['restore_ok']
    assert len(calls) == 1 and not docs[0].closed
    assert br.TRANSFER_PENDING.is_set()
    assert settings['exposure_ms'] == 800. and flags['EXP_AUTOSAVE'] == -1
    assert 'summary' not in report


def test_uncertain_stop_blocks_restore_and_retains_owner(tmp_path):
    probe = load_probe()
    br, exp, settings, flags, events, _, _ = parameter_bridge(tmp_path)
    def stalled(*a, **k):
        br.TEMPERATURE_MONITOR_UNHEALTHY.set()
        raise RuntimeError('uncertain Stop')
    br.acquire = stalled
    report = probe.run_probe(br, str(tmp_path), lambda r: None, pairs=1, measured=1)
    assert report['status'] == 'failed' and not report['restore_ok']
    assert br._setparam_probe_owner is exp
    assert settings['exposure_ms'] == 500. and flags['EXP_AUTOSAVE'] == 0
    assert not any(e[0] == 'write' and e[2] == -1 and e[1] == 'EXP_AUTOSAVE' for e in events)


def test_restore_failure_still_attempts_other_independent_restorations(tmp_path):
    probe = load_probe()
    br, _, _, flags, _, _, _ = parameter_bridge(tmp_path)
    apply = br.apply_settings
    def fail_restore(e, values):
        if values['exposure_ms'] == 800.:
            raise RuntimeError('restore failed')
        return apply(e, values)
    br.apply_settings = fail_restore
    report = probe.run_probe(br, str(tmp_path), lambda r: None, pairs=1, measured=1)
    assert report['status'] == 'failed' and not report['restore_ok']
    assert flags['EXP_AUTOSAVE'] == -1 and flags['EXP_BSHOWWINDOW'] == -1
    assert 'summary' not in report


def test_actual_v12_autosave_helper_uses_proxy_and_still_verifies_readback(tmp_path):
    from tests.test_winspec_bridge_guard import bridge_functions
    probe = load_probe()
    br, exp, _, flags, events, _, _ = parameter_bridge(tmp_path)
    flags['EXP_AUTOSAVE'] = 0
    ns = bridge_functions(dict(const=br.const, com_return_value=br.com_return_value,
        read_parameter=lambda e, key: br.get_param(e, key)), 'disable_acquisition_autosave')
    wrapped = probe.ParameterExperiment(br, exp, skip_autosave=True)
    ns['disable_acquisition_autosave'](wrapped)
    assert events == [] and wrapped.writes[0]['skipped']
    flags['EXP_AUTOSAVE'] = -1
    ns['disable_acquisition_autosave'](wrapped)
    assert events == [('write', 'EXP_AUTOSAVE', False)]


def test_final_archive_mutation_invalidates_timings(tmp_path):
    probe = load_probe()
    br, _, _, _, _, _, _ = parameter_bridge(tmp_path)
    read = br.read_spe_frames
    seen = set()
    def mutated(path):
        value = read(path)
        if Path(path).name == 'archive-001.spe':
            if path in seen:
                return value[:4] + (b'X' * len(value[4]), value[5])
            seen.add(path)
        return value
    br.read_spe_frames = mutated
    report = probe.run_probe(br, str(tmp_path), lambda r: None, pairs=1, measured=1)
    assert report['status'] == 'failed' and report['restore_ok']
    assert 'archive changed' in report['error'] and 'summary' not in report


def test_failed_persistence_never_acknowledges_frame(tmp_path):
    probe = load_probe()
    br, _, _, _, _, docs, _ = parameter_bridge(tmp_path)
    def persist(report):
        if report['frames']:
            raise OSError('report disk full')
    report = probe.run_probe(br, str(tmp_path), persist, pairs=1, measured=1)
    assert report['status'] == 'failed' and report['restore_ok']
    assert len(docs) == 1 and not docs[0].closed and br.TRANSFER_PENDING.is_set()
    assert 'summary' not in report


def test_unexpected_parameter_write_sequence_rejects_the_comparison(tmp_path):
    probe = load_probe()
    br, exp, _, _, _, docs, _ = parameter_bridge(tmp_path)
    capture = br.acquire
    br.acquire = lambda ignored, **kwargs: capture(exp, **kwargs)
    report = probe.run_probe(br, str(tmp_path), lambda r: None, pairs=1, measured=1)
    assert report['status'] == 'failed' and report['restore_ok']
    assert 'write sequence' in report['error']
    assert not docs[0].closed and br.TRANSFER_PENDING.is_set()


def test_cleanup_failure_keeps_document_alive_with_pending_tuple(tmp_path):
    import gc
    import weakref
    probe = load_probe()
    br, _, _, _, _, docs, calls = parameter_bridge(tmp_path)
    finish, refs = br.finish_transfer, []
    def fail_close(sock, sent):
        owned = br.TRANSFER_STATE.pending[0]
        refs.append(weakref.ref(owned))
        owned.Save = lambda: False
        docs.clear()
        calls.clear()
        return finish(sock, sent)
    br.finish_transfer = fail_close
    report = probe.run_probe(br, str(tmp_path), lambda r: None, pairs=1, measured=1)
    gc.collect()
    assert report['status'] == 'failed' and br.TRANSFER_STATE.pending is not None
    assert refs[0]() is not None, 'COM document owner was released after failed cleanup'
    assert br._setparam_probe_documents and probe.recovery_required(br)


def test_start_exception_retains_supplied_document_even_without_pending_tuple(tmp_path):
    import gc
    import weakref
    probe = load_probe()
    br, exp, _, _, _, _, _ = parameter_bridge(tmp_path)
    refs = []
    class EphemeralDoc:
        pass
    def acquire(e, **kwargs):
        doc = EphemeralDoc()
        refs.append(weakref.ref(doc))
        e.Start(doc)
    def failed_start(doc):
        raise RuntimeError('Start failed')
    exp.Start = failed_start
    br.acquire = acquire
    report = probe.run_probe(br, str(tmp_path), lambda r: None, pairs=1, measured=1)
    gc.collect()
    assert report['status'] == 'failed' and br.TRANSFER_STATE.pending is None
    assert refs[0]() is not None and probe.recovery_required(br)


def test_quit_releases_application_interface_before_caller_uninitializes_com(tmp_path):
    from types import SimpleNamespace
    probe = load_probe()
    br, _, _, _, _, _, _ = parameter_bridge(tmp_path)
    events = []
    class App:
        def CountOpenDocs(self):
            return 0
        def Quit(self):
            events.append('Quit')
            return True
        def __del__(self):
            events.append('release_application')
    result = probe.quit_owned_application(br, SimpleNamespace(Dispatch=lambda _: App()))
    events.append('CoUninitialize')
    assert result is True and events == ['Quit', 'release_application', 'CoUninitialize']


@pytest.mark.parametrize('documents,quit_result', [(1, True), (0, False)])
def test_incomplete_quit_retains_application_and_blocks_apartment_teardown(tmp_path, documents, quit_result):
    from types import SimpleNamespace
    probe = load_probe()
    br, _, _, _, _, _, _ = parameter_bridge(tmp_path)
    client = SimpleNamespace(Dispatch=lambda _: SimpleNamespace(CountOpenDocs=lambda: documents, Quit=lambda: quit_result))
    with pytest.raises(RuntimeError):
        probe.quit_owned_application(br, client)
    assert probe.recovery_required(br) and br._setparam_probe_application is not None


def test_process_inventory_returns_only_values_and_releases_wmi_scope():
    import gc
    import weakref
    from types import SimpleNamespace
    probe = load_probe()
    refs = []
    class Process:
        ProcessId = 42
        Name = 'Explorer.EXE'
    class WMI:
        def InstancesOf(self, name):
            assert name == 'Win32_Process'
            process = Process()
            refs.append(weakref.ref(process))
            return [process]
    result = probe.snapshot_processes(SimpleNamespace(GetObject=lambda _: WMI()))
    gc.collect()
    assert result == [(42, 'explorer.exe')] and refs[0]() is None
