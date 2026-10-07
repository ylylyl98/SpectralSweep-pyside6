"""Isolated WinSpec v12 SetParam experiment, compatible with XP Python 2.7.

No production bridge replacement. Two independent, alternating-block comparisons:
skip verified no-op autosave writes; hold display hidden across a block.
"""
from __future__ import print_function

import hashlib
import json
import os
import shutil
import sys
import time

try:
    from .startup_reuse_probe import clock, median, reserve_bridge_port, validate_frame, persist_reports
    from .startup_display_probe import archive_spe, LocalReceipt
except (ImportError, ValueError):
    from startup_reuse_probe import clock, median, reserve_bridge_port, validate_frame, persist_reports
    from startup_display_probe import archive_spe, LocalReceipt


def read_boolean(br, exp, key):
    value = br.get_param(exp, key)
    if value not in (-1, 0, 1) or value is None:
        raise RuntimeError('Boolean parameter unavailable or invalid: %r' % key)
    return value


def write_boolean(br, exp, key, value):
    status = br.com_return_value(exp.SetParam(key, value), 'SetParam')
    if status not in (0, False) or bool(read_boolean(br, exp, key)) != bool(value):
        raise RuntimeError('Boolean parameter write/readback failed: %r' % key)


def require_idle(settings):
    for key in ('running', 'winspec_reported_running'):
        if settings.get(key) is not False:
            raise RuntimeError('WinSpec idle state not confirmed: ' + key)
    if settings.get('controller_running', False) is not False:
        raise RuntimeError('WinSpec controller idle state not confirmed')


def retain_document(br, document):
    if document is not None and not any(document is old for old in br._setparam_probe_documents):
        br._setparam_probe_documents.append(document)


def recovery_required(br):
    return (br.TEMPERATURE_MONITOR_UNHEALTHY.is_set() or br.TRANSFER_PENDING.is_set() or
            br.CLEANUP_FAILED.is_set() or bool(getattr(br, '_setparam_probe_documents', [])) or
            bool(getattr(br, '_setparam_probe_recovery', False)))


class ParameterExperiment(object):
    """Forward COM calls; suppress only freshly verified equal autosave writes."""
    def __init__(self, br, exp, skip_autosave=False):
        self.br, self.exp, self.skip_autosave = br, exp, skip_autosave
        self.writes = []
        self.keys = dict((br.const(name), name) for name in ('EXP_AUTOSAVE', 'EXP_BSHOWWINDOW'))

    def __getattr__(self, name):
        return getattr(self.exp, name)

    def Start(self, document):
        retain_document(self.br, document)
        result = self.exp.Start(document)
        if isinstance(result, (tuple, list)) and len(result) > 1:
            retain_document(self.br, self.br.start2_document(result[1:]))
        return result

    def SetParam(self, key, value):
        skipped = False
        if self.skip_autosave and key == self.br.const('EXP_AUTOSAVE'):
            if value not in (-1, 0, 1):
                raise RuntimeError('Boolean autosave request invalid')
            skipped = bool(read_boolean(self.br, self.exp, key)) == bool(value)
        result = 0 if skipped else self.exp.SetParam(key, value)
        self.writes.append(dict(key=self.keys.get(key, str(key)), value=value, skipped=skipped))
        return result


def summarize(frames):
    result = {}
    for experiment in ('autosave', 'display'):
        rows = [f for f in frames if f['experiment'] == experiment and not f['warmup']]
        group = {}
        for arm in ('baseline', 'optimized'):
            selected = [f for f in rows if f['arm'] == arm]
            group[arm] = dict(n=len(selected),
                start_median_s=median([f['bridge_timing_s']['start_experiment'] for f in selected]),
                capture_median_s=median([f['capture_s'] for f in selected]))
        deltas = []
        for pair in sorted(set(f['pair'] for f in rows)):
            timings = [median([f['bridge_timing_s']['start_experiment'] for f in rows
                               if f['pair'] == pair and f['arm'] == arm])
                       for arm in ('baseline', 'optimized')]
            deltas.append(timings[0] - timings[1])
        group['paired_start_saved_s'] = deltas
        group['paired_start_saved_median_s'] = median(deltas)
        result[experiment] = group
    return result


def validate_writes(writes, skip_auto, held_display):
    expected = [('EXP_AUTOSAVE', False, skip_auto)]
    if not held_display:
        expected.extend([('EXP_BSHOWWINDOW', False, False), ('EXP_BSHOWWINDOW', True, False)])
    expected.append(('EXP_AUTOSAVE', False, skip_auto))
    actual = [(row['key'], bool(row['value']), row['skipped']) for row in writes]
    if actual != expected:
        raise RuntimeError('Unexpected parameter write sequence: %r' % actual)


def run_probe(br, output_dir, persist, pairs=5, measured=2):
    if not 1 <= pairs <= 10 or not 1 <= measured <= 5:
        raise ValueError('Probe bounds exceeded')
    exp = br.create_experiment()
    br._setparam_probe_owner = exp  # Remains owned on every uncertain failure.
    br._setparam_probe_documents = []
    br._setparam_probe_recovery = False
    original = br.read_settings(exp)
    require_idle(original)
    br.validate_acquisition_request(original)
    br.validate_temperature(original)
    if br.TEMPERATURE_MONITOR_UNHEALTHY.is_set() or br.TRANSFER_PENDING.is_set() or br.CLEANUP_FAILED.is_set():
        raise RuntimeError('Previous acquisition requires recovery')
    keys = dict((name, br.const(name)) for name in ('EXP_AUTOSAVE', 'EXP_BSHOWWINDOW', 'EXP_NEWWINDOW'))
    if any(value is None for value in keys.values()):
        raise RuntimeError('Required Boolean parameter not supported')
    flags = dict((name, read_boolean(br, exp, key)) for name, key in keys.items())
    if not flags['EXP_BSHOWWINDOW']:
        raise RuntimeError('Display already hidden; requested display comparison is not applicable')
    restore = dict((key, original[key]) for key in ('exposure_ms', 'accumulations', 'sequential_frames'))
    old_path = br.SPE_PATH
    report = dict(status='running', restore_ok=False, original_settings=original, original_flags=flags,
                  frames=[], pairs=pairs, measured_per_block=measured,
                  scope='Isolated v12; fresh documents; 500 ms x 2; one warmup per block; Start is primary endpoint',
                  timing_excludes='Independent archives, progress reports, cleanup, block preparation and network')
    expected = None
    original_create = getattr(br, 'create_document', None)
    if original_create is not None:
        def create_owned_document():
            doc = original_create()
            retain_document(br, doc)
            return doc
        br.create_document = create_owned_document
    try:
        persist(report)
        br.SPE_PATH = os.path.join(output_dir, 'temporary.spe')
        expected = br.apply_settings(exp, dict(exposure_ms=500., accumulations=2, sequential_frames=1))
        if any(expected.get(k) != v for k, v in (('exposure_ms', 500.), ('accumulations', 2), ('sequential_frames', 1))):
            raise RuntimeError('Acquisition recipe readback mismatch')
        br.validate_acquisition_request(expected)
        br.validate_temperature(expected)
        # Same false autosave state for both arms; only repeated writes differ.
        write_boolean(br, exp, keys['EXP_AUTOSAVE'], 0)
        for experiment in ('autosave', 'display'):
            for pair in range(1, pairs + 1):
                order = ('baseline', 'optimized') if pair % 2 else ('optimized', 'baseline')
                for arm in order:
                    hold_hidden = experiment == 'display' and arm == 'optimized'
                    visible = 0 if hold_hidden else flags['EXP_BSHOWWINDOW']
                    write_boolean(br, exp, keys['EXP_BSHOWWINDOW'], visible)
                    wrapped = ParameterExperiment(br, exp, skip_autosave=(experiment == 'autosave' and arm == 'optimized'))
                    for index in range(measured + 1):
                        wrapped.writes = []
                        started = clock()
                        metadata, raw = br.acquire(wrapped, compact_settings=True, expected_settings=expected)
                        duration = clock() - started
                        pending = getattr(br.TRANSFER_STATE, 'pending', None)
                        if pending is not None:
                            retain_document(br, pending[0])
                        values = validate_frame(metadata, raw, expected)
                        br.validate_temperature(metadata['settings'])
                        validate_writes(wrapped.writes, wrapped.skip_autosave, hold_hidden)
                        for name, value in (('EXP_AUTOSAVE', 0), ('EXP_BSHOWWINDOW', visible), ('EXP_NEWWINDOW', flags['EXP_NEWWINDOW'])):
                            if bool(read_boolean(br, exp, keys[name])) != bool(value):
                                raise RuntimeError('Unexpected parameter change: ' + name)
                        pending = getattr(br.TRANSFER_STATE, 'pending', None)
                        if pending is None:
                            raise RuntimeError('Owned acquisition document missing')
                        doc, source = pending
                        archive = os.path.join(output_dir, 'archive-%03d.spe' % (len(report['frames']) + 1))
                        archive_spe(br, source, archive, metadata, raw, expected)
                        frame = dict(experiment=experiment, arm=arm, pair=pair, warmup=(index == 0),
                            capture_s=duration, bridge_timing_s=metadata['bridge_timing_s'],
                            parameter_writes=list(wrapped.writes), metadata=metadata,
                            spe_archive=archive, temporary_spe=source, counts=list(values),
                            sha256=hashlib.sha256(raw).hexdigest())
                        report['frames'].append(frame)
                        persist(report)  # Must succeed before document Save/Close/delete.
                        if br.finish_transfer(LocalReceipt(), True) is not True:
                            raise RuntimeError('Owned document cleanup failed')
                        br._setparam_probe_documents[:] = []
                        doc = None
                        print('%s %s pair %d %s: Start %.6f s, capture %.6f s' %
                              (experiment, arm, pair, 'warmup' if index == 0 else 'measured',
                               frame['bridge_timing_s']['start_experiment'], duration))
                        sys.stdout.flush()
        for frame in report['frames']:
            width, height, count, dtype, raw, hardware = br.read_spe_frames(frame['spe_archive'])
            br.settings_from_spe(expected, hardware, width, height, count, {})
            if dtype != frame['metadata']['winspec_datatype'] or hashlib.sha256(raw).hexdigest() != frame['sha256']:
                raise RuntimeError('Independent SPE archive changed')
        report['status'] = 'complete'
    except BaseException as error:
        report.update(status='failed', error=type(error).__name__ + ': ' + str(error))
    finally:
        br.SPE_PATH = old_path
        if original_create is not None:
            br.create_document = original_create
        errors = {}
        if br.TEMPERATURE_MONITOR_UNHEALTHY.is_set():
            errors['ownership'] = 'Uncertain Stop; retain COM owner and port; no restoration writes'
        else:
            try:
                require_idle(br.read_settings(exp))
            except BaseException as error:
                br.TEMPERATURE_MONITOR_UNHEALTHY.set()
                errors['ownership'] = 'Idle not confirmed; no restoration writes: ' + str(error)
        if not errors:
            try:
                restored = br.apply_settings(exp, restore)
                if any(restored.get(k) != v for k, v in restore.items()):
                    raise RuntimeError('Recipe restoration mismatch')
                require_idle(restored)
                br.validate_temperature(restored)
                report['restored_settings'] = restored
            except BaseException as error:
                errors['recipe'] = str(error)
            for name in ('EXP_AUTOSAVE', 'EXP_BSHOWWINDOW'):
                try:
                    write_boolean(br, exp, keys[name], flags[name])
                except BaseException as error:
                    errors[name] = str(error)
            try:
                report['restored_flags'] = dict((name, read_boolean(br, exp, key)) for name, key in keys.items())
                if report['restored_flags'] != flags:
                    raise RuntimeError('Final flags do not match original settings')
            except BaseException as error:
                errors['flags'] = str(error)
        report['restore_ok'] = not errors
        if errors:
            br._setparam_probe_recovery = True
            report.update(status='failed', restore_errors=errors)
        if report['status'] == 'complete':
            report['summary'] = summarize(report['frames'])
        try:
            persist(report)
        except BaseException as error:
            report.update(status='failed', report_write_error=str(error))
            report.pop('summary', None)
    return report


def quit_owned_application(br, client):
    app = client.Dispatch('{675DA983-188A-11D1-9330-444553540000}')
    try:
        if br.com_return_value(app.CountOpenDocs(), 'CountOpenDocs') != 0:
            raise RuntimeError('Open documents remain; WinSpec left open')
        br._setparam_probe_owner = None
        result = br.com_return_value(app.Quit(), 'Quit')
        if not result:
            raise RuntimeError('WinSpec Quit did not confirm success')
        return True
    except BaseException:
        br._setparam_probe_application = app
        br._setparam_probe_recovery = True
        raise
    finally:
        app = None  # Release while the caller's COM apartment is still initialized.


def snapshot_processes(client):
    # Python 2 leaks a comprehension's loop variable into its enclosing scope.
    # Keep every WMI COM object inside this helper, before main uninitializes COM.
    return [(int(p.ProcessId), str(p.Name).lower()) for p in
            client.GetObject('winmgmts:').InstancesOf('Win32_Process')]


def main(pairs=5, measured=2):
    lease = reserve_bridge_port()
    import pythoncom
    pythoncom.CoInitialize()
    br = None
    try:
        import win32com.client
        processes = snapshot_processes(win32com.client)
        if any(name in ('winspec.exe', 'python.exe', 'pythonw.exe') and pid != os.getpid()
               for pid, name in processes):
            raise RuntimeError('Existing WinSpec/Python owner found; not taking over')
        import camera_server as br
        if (br.SERVER_BUILD != '2026-10-01-display-performance-v12' or
                not callable(getattr(br, 'confirmed_stop', None))):
            raise RuntimeError('Probe requires the frozen v12 diagnostic bridge with Stop ownership protection')
        stamp = time.strftime('%Y%m%d-%H%M%S') + '-%d' % os.getpid()
        output = os.path.join(r'C:\WinSpecRemote', 'setparam-probe-' + stamp)
        os.makedirs(output)
        shared = os.path.dirname(os.path.abspath(__file__))
        shared_report = os.path.join(shared, 'setparam-result-' + stamp + '.json')
        def persist(report):
            report['server_build'] = br.SERVER_BUILD
            persist_reports(report, output, shared_report)
        report = run_probe(br, output, persist, pairs=pairs, measured=measured)
        # Export independent files; no source path is ever supplied back to WinSpec.
        shutil.copytree(output, os.path.join(shared, 'archives-' + stamp))
        if report['status'] == 'complete' and report['restore_ok']:
            quit_result = quit_owned_application(br, win32com.client)
            with open(os.path.join(shared, 'shutdown-' + stamp + '.json'), 'wb') as handle:
                handle.write(json.dumps(dict(quit_return=quit_result, restore_ok=True)).encode('ascii'))
            return 0
        return 1
    finally:
        if br is not None and recovery_required(br):
            print('RECOVERY REQUIRED: owner and port retained; no further camera calls')
            sys.stdout.flush()
            while True:
                try:
                    time.sleep(1)
                except BaseException:
                    pass
        pythoncom.CoUninitialize()
        lease.close()


if __name__ == '__main__':
    if len(sys.argv) == 1:
        sys.exit(main())
    if len(sys.argv) != 3:
        raise ValueError('Expected optional pair count and measured frames per block')
    sys.exit(main(int(sys.argv[1]), int(sys.argv[2])))
