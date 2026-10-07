from __future__ import print_function

"""Offline display A/B; fresh documents and independent durable SPE archives."""
import hashlib
import json
import os
import sys
import time

try:
    from .startup_reuse_probe import clock, median, reserve_bridge_port, validate_frame, persist_reports
except (ImportError, ValueError):
    from startup_reuse_probe import clock, median, reserve_bridge_port, validate_frame, persist_reports


class LocalReceipt(object):
    """Only acknowledge after validated raw data, SPE archive and counts are durable."""
    def settimeout(self, seconds):
        pass

    def recv(self, size):
        return b'WXAK'[:size]


def set_display(br, exp, key, value):
    result = br.com_return_value(exp.SetParam(key, value), 'SetParam')
    if result not in (0, False):
        raise RuntimeError('Display SetParam failed with status %r' % result)
    actual = br.get_param(exp, key)
    if actual not in (-1, 0, 1) or bool(actual) != bool(value):
        raise RuntimeError('Display setting readback mismatch')
    return actual


def archive_spe(br, source, target, metadata, raw, expected):
    # WinSpec never sees this path. Later Save/Close cannot retarget the archive.
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, 'O_BINARY', 0)
    descriptor = os.open(target, flags, 0o600)
    with os.fdopen(descriptor, 'wb') as destination:
        with open(source, 'rb') as incoming:
            while True:
                chunk = incoming.read(65536)
                if not chunk:
                    break
                destination.write(chunk)
        destination.flush()
        os.fsync(destination.fileno())
    width, height, count, datatype, archived, hardware = br.read_spe_frames(target)
    br.settings_from_spe(expected, hardware, width, height, count, {})
    if datatype != metadata['winspec_datatype'] or archived != raw:
        raise RuntimeError('Independent SPE archive does not match acquired data')


def run_probe(br, output_dir, persist):
    exp = br.create_experiment()
    original = br.read_settings(exp)
    if any(original.get(k) for k in ('running', 'controller_running', 'winspec_reported_running')):
        raise RuntimeError('WinSpec is busy; no settings changed')
    br.validate_acquisition_request(original)
    br.validate_temperature(original)
    if br.TEMPERATURE_MONITOR_UNHEALTHY.is_set():
        raise RuntimeError('WinSpec Stop is stalled; do not run the probe')
    display_key = br.const('EXP_BSHOWWINDOW')
    window_key = br.const('EXP_NEWWINDOW')
    if display_key is None or window_key is None:
        raise RuntimeError('Installed WinSpec does not expose display/window flags')
    original_display = br.get_param(exp, display_key)
    original_newwindow = br.get_param(exp, window_key)
    if original_display not in (-1, 1):
        raise RuntimeError('Display already disabled or unsupported; visible/hidden comparison not applicable')
    restore = dict((k,original[k]) for k in ('exposure_ms','accumulations','sequential_frames'))
    old_path = br.SPE_PATH
    report = dict(status='running', restore_ok=False, original_settings=original,
                  original_display=original_display, original_newwindow=original_newwindow,
                  scope='500 ms x 2; only display flag varies; fresh documents; no LightField/SMU', frames=[])

    def capture(mode, pair, expected):
        visible = mode in ('visible', 'warmup_visible')
        actual = set_display(br, exp, display_key, original_display if visible else 0)
        started = clock()
        metadata, raw = br.acquire(exp, compact_settings=True, expected_settings=expected)
        duration = clock() - started
        values = validate_frame(metadata, raw, expected)
        br.validate_temperature(metadata['settings'])
        pending = getattr(br.TRANSFER_STATE, 'pending', None)
        if pending is None:
            raise RuntimeError('Display probe lost its owned document')
        doc, source = pending
        if bool(br.get_param(exp, display_key)) != visible:
            raise RuntimeError('Display setting changed during acquisition')
        if br.get_param(exp, window_key) != original_newwindow:
            raise RuntimeError('New-window flag changed during acquisition')
        archive = os.path.join(output_dir, 'archive-%02d-%s.spe' % (len(report['frames'])+1, mode))
        archive_spe(br, source, archive, metadata, raw, expected)
        frame = dict(mode=mode, pair=pair, capture_s=duration, display_readback=actual,
                     spe_archive=archive, temporary_spe=source, winspec_datatype=metadata['winspec_datatype'],
                     bridge_timing_s=metadata['bridge_timing_s'], settings=metadata['settings'],
                     temperature_guard=metadata['temperature_guard'], counts=list(values),
                     sha256=hashlib.sha256(raw).hexdigest(),
                     mean_counts_per_exposure=sum(values)/float(512*expected['accumulations']))
        report['frames'].append(frame)
        persist(report)
        if br.finish_transfer(LocalReceipt(), True) is not True:
            raise RuntimeError('Owned document cleanup failed; recover retained data')
        print('%s pair %d: Start %.4f s, capture %.4f s' %
              (mode, pair, frame['bridge_timing_s']['start_experiment'], duration))
        sys.stdout.flush()

    try:
        persist(report)  # Preserve original state before the first settings write.
        br.SPE_PATH = os.path.join(output_dir, 'temporary.spe')
        expected = br.apply_settings(exp, dict(exposure_ms=500., accumulations=2, sequential_frames=1))
        if any(expected.get(k) != v for k,v in (('exposure_ms',500.),('accumulations',2),('sequential_frames',1))):
            raise RuntimeError('Display probe acquisition setting readback mismatch')
        br.validate_acquisition_request(expected)
        br.validate_temperature(expected)
        for mode in ('warmup_visible', 'warmup_hidden'):
            capture(mode, 0, expected)
        for pair in range(1, 6):
            order = ('visible','hidden') if pair % 2 else ('hidden','visible')
            for mode in order:
                capture(mode, pair, expected)
        for frame in report['frames']:
            width, height, count, datatype, raw, hardware = br.read_spe_frames(frame['spe_archive'])
            br.settings_from_spe(expected, hardware, width, height, count, {})
            if datatype != frame['winspec_datatype'] or hashlib.sha256(raw).hexdigest() != frame['sha256']:
                raise RuntimeError('Independent SPE archive changed: ' + frame['spe_archive'])
        summary = {}
        for mode in ('visible', 'hidden'):
            frames = [f for f in report['frames'] if f['mode'] == mode]
            summary[mode] = dict(start_median_s=median([f['bridge_timing_s']['start_experiment'] for f in frames]),
                                 capture_median_s=median([f['capture_s'] for f in frames]))
        report['summary'] = summary
        report['start_saved_s'] = summary['visible']['start_median_s'] - summary['hidden']['start_median_s']
        report['capture_saved_s'] = summary['visible']['capture_median_s'] - summary['hidden']['capture_median_s']
        report['status'] = 'complete'
    except BaseException as error:
        report['status'] = 'failed'
        report['error'] = '%s: %s' % (type(error).__name__, error)
        if not br.TEMPERATURE_MONITOR_UNHEALTHY.is_set():
            try:
                exp.Stop()
            except BaseException as stop_error:
                br.TEMPERATURE_MONITOR_UNHEALTHY.set()
                report['stop_error'] = str(stop_error)
    finally:
        br.SPE_PATH = old_path
        errors = {}
        if br.TEMPERATURE_MONITOR_UNHEALTHY.is_set():
            errors['stalled'] = 'Stop is stalled; all restoration writes skipped until recovery'
        else:
            # Attempt each restoration independently: a display failure must not
            # prevent restoring the exposure recipe, or vice versa.
            try:
                restored = br.apply_settings(exp, restore)
                if any(restored.get(k) != v for k,v in restore.items()):
                    raise RuntimeError('Exposure settings restoration mismatch')
                br.validate_temperature(restored)
                report['restored_settings'] = restored
            except BaseException as error:
                errors['acquisition'] = str(error)
            try:
                report['restored_display'] = set_display(br, exp, display_key, original_display)
                if br.get_param(exp, window_key) != original_newwindow:
                    raise RuntimeError('New-window flag changed externally; not restored automatically')
            except BaseException as error:
                errors['display'] = str(error)
        report['restore_ok'] = not errors
        if errors:
            report['restore_errors'] = errors
            report['status'] = 'failed'
        try:
            persist(report)
        except BaseException as error:
            report['status'] = 'failed'
            report['report_write_error'] = str(error)
    return report


def main():
    lease = reserve_bridge_port()
    try:
        sys.path.insert(0, r'C:\WinSpecRemote')
        import camera_server as br
        if br.ACQUISITION_SETTINGS_VERSION != 2 or 'startup-performance-v11' not in br.SERVER_BUILD:
            raise RuntimeError('Install the verified v11 bridge before this probe')
        stamp = time.strftime('%Y%m%d-%H%M%S') + '-%d' % os.getpid()
        output_dir = os.path.join(r'C:\WinSpecRemote', 'display-probe-' + stamp)
        os.makedirs(output_dir)
        shared = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'display-result-' + stamp + '.json')
        def persist(report):
            report['server_build'] = br.SERVER_BUILD
            persist_reports(report, output_dir, shared)
        br.pythoncom.CoInitialize()
        try:
            report = run_probe(br, output_dir, persist)
        finally:
            br.TRANSFER_STATE.pending = None
            br.pythoncom.CoUninitialize()
        print(json.dumps(dict((k,report.get(k)) for k in
              ('status','summary','start_saved_s','capture_saved_s','restore_ok','error','restore_errors')), indent=2))
        print('Local archives: ' + output_dir)
        print('Shared report: ' + shared)
        return 0 if report['status'] == 'complete' and report['restore_ok'] else 1
    finally:
        lease.close()


if __name__ == '__main__':
    try:
        sys.exit(main())
    except Exception as error:
        print('DISPLAY PROBE ABORTED: ' + str(error))
        sys.exit(1)
