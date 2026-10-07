from __future__ import print_function

"""Offline XP A/B probe. Normal bridge acquisition is not modified on disk."""
import hashlib
import json
import math
import os
import shutil
import socket
import struct
import sys
import time


def clock():
    return (getattr(time, 'monotonic', None) or time.clock)()


def median(values):
    values = sorted(values)
    middle = len(values) // 2
    return values[middle] if len(values) % 2 else (values[middle-1] + values[middle]) / 2.


def reserve_bridge_port(port=5000):
    """Keep the normal server and a second probe from starting during the test."""
    lease = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        if hasattr(socket, 'SO_EXCLUSIVEADDRUSE'):
            lease.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        lease.bind(('0.0.0.0', port))
    except Exception:
        lease.close()
        raise RuntimeError('Close only the camera SERVER window first; its port is occupied')
    return lease


def validate_frame(metadata, raw, expected):
    if (metadata.get('width'), metadata.get('height'), metadata.get('frame_count')) != (512, 1, 1):
        raise RuntimeError('Probe frame geometry mismatch')
    guard = metadata.get('temperature_guard', {})
    if guard.get('version') != 4 or guard.get('passed') is not True or guard.get('sample_count', 0) < 2:
        raise RuntimeError('Probe frame temperature protection missing')
    actual = metadata.get('settings', {})
    for key in ('exposure_ms', 'accumulations'):
        value = actual.get(key)
        if (isinstance(value, bool) or not isinstance(value, (int, float))
                or math.isnan(value) or math.isinf(value)
                or abs(value - expected[key]) > (0 if key == 'accumulations' else .001)):
            raise RuntimeError('Probe SPE %s mismatch' % key)
    if metadata.get('settings_scope') != 'configured_plus_spe':
        raise RuntimeError('Probe needs SPE-verified acquisition')
    code = {0:'f', 1:'i', 2:'h', 3:'H', 5:'d', 6:'B'}.get(metadata.get('winspec_datatype'))
    if code is None or len(raw) != 512 * struct.calcsize('<' + code):
        raise RuntimeError('Probe frame data size/type mismatch')
    values = struct.unpack('<512' + code, raw)
    if any(math.isnan(v) or math.isinf(v) for v in values):
        raise RuntimeError('Probe frame contains non-finite counts')
    return values


def persist_reports(report, output_dir, shared_report):
    # Never truncate the durable report for a frame already accepted/reused.
    # Each progress state has its own exclusive-create, flushed local snapshot.
    snapshot = os.path.join(output_dir, 'frame-%03d-%s.json' %
                            (len(report['frames']), report['status']))
    descriptor = os.open(snapshot, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, 'w') as handle:
        json.dump(report, handle, indent=2, allow_nan=False)
        handle.flush()
        os.fsync(handle.fileno())
    # Convenience copies may fail; durable snapshots and all SPEs remain intact.
    shutil.copyfile(snapshot, os.path.join(output_dir, 'result.json'))
    shutil.copyfile(snapshot, shared_report)


def run_probe(br, output_dir, persist):
    """Caller owns the port lease and COM apartment for this entire function."""
    exp = br.create_experiment()
    original = br.read_settings(exp)
    if original.get('running') or original.get('winspec_reported_running') or original.get('controller_running'):
        raise RuntimeError('WinSpec is busy; no settings changed')
    br.validate_acquisition_request(original)
    br.validate_temperature(original)
    if br.TEMPERATURE_MONITOR_UNHEALTHY.is_set():
        raise RuntimeError('WinSpec Stop is stalled; do not run the probe')
    restore = dict((k, original[k]) for k in ('exposure_ms', 'accumulations', 'sequential_frames'))
    old_path, old_start = br.SPE_PATH, br.start_new_document
    selected = [None]
    report = dict(status='running', restore_ok=False, original_settings=original,
                  scope='Offline startup A/B, no LightField or SMU; all SPE files retained', frames=[])
    owned_doc = None

    def start(exp, timings=None):
        if selected[0] is None:
            return old_start(exp, timings=timings)
        doc = selected[0]
        started = clock()
        result = exp.Start(doc)
        if timings is not None:
            timings['create_document'] = 0.
            timings['start_experiment'] = clock() - started
        if not br.com_return_value(result, 'Start'):
            raise RuntimeError('WinSpec could not start into the owned probe document')
        actual = br.start2_document(result[1:]) if isinstance(result, (tuple, list)) and len(result) > 1 else doc
        # COM identity uses IUnknown, not Python wrapper identity or the active UI.
        identity = br.pythoncom.IID_IUnknown
        if actual._oleobj_.QueryInterface(identity) != doc._oleobj_.QueryInterface(identity):
            raise RuntimeError('WinSpec returned a different document during the reuse test')
        return actual

    def capture(mode, index, expected, document=None):
        selected[0] = document
        started = clock()
        metadata, raw = br.acquire(exp, compact_settings=True, expected_settings=expected)
        duration = clock() - started
        values = validate_frame(metadata, raw, expected)
        br.validate_temperature(metadata['settings'])
        pending = getattr(br.TRANSFER_STATE, 'pending', None)
        if pending is None:
            raise RuntimeError('Probe acquisition lost its owned document')
        doc, path = pending
        if not br.com_return_value(doc.Save(), 'Save'):
            raise RuntimeError('Probe document save failed; retaining its SPE')
        frame = dict(mode=mode, index=index, capture_s=duration,
                     reuse_identity_confirmed=(mode == 'reuse'),
                     winspec_datatype=metadata['winspec_datatype'],
                     bridge_timing_s=metadata['bridge_timing_s'],
                     settings=metadata['settings'], temperature_guard=metadata['temperature_guard'],
                     spe_path=path, counts=list(values), sha256=hashlib.sha256(raw).hexdigest(),
                     mean_counts_per_exposure=sum(values) / float(512 * expected['accumulations']))
        report['frames'].append(frame)
        # A completed, verified frame is durable locally before any reuse/close.
        persist(report)
        br.TRANSFER_STATE.pending = None
        br.TRANSFER_PENDING.clear()
        print('%s %d: Start %.4f s, capture %.4f s' %
              (mode, index, frame['bridge_timing_s']['start_experiment'], duration))
        sys.stdout.flush()
        return doc

    try:
        br.SPE_PATH = os.path.join(output_dir, 'probe_frame.spe')
        br.start_new_document = start
        expected = br.apply_settings(exp, dict(exposure_ms=500., accumulations=2, sequential_frames=1))
        for key, value in (('exposure_ms',500.), ('accumulations',2), ('sequential_frames',1)):
            if expected.get(key) != value:
                raise RuntimeError('Probe Apply readback mismatch: ' + key)
        br.validate_acquisition_request(expected)
        br.validate_temperature(expected)
        owned_doc = capture('warmup', 0, expected)
        for index in range(1, 6):
            fresh = capture('fresh', index, expected)
            if not br.com_return_value(fresh.Close(), 'Close'):
                raise RuntimeError('Fresh probe document did not close')
            owned_doc = capture('reuse', index, expected, owned_doc)
        if not br.com_return_value(owned_doc.Close(), 'Close'):
            raise RuntimeError('Reusable probe document did not close')
        for frame in report['frames']:
            width, height, count, datatype, raw, hardware = br.read_spe_frames(frame['spe_path'])
            br.settings_from_spe(expected, hardware, width, height, count, {})
            if datatype != frame['winspec_datatype'] or hashlib.sha256(raw).hexdigest() != frame['sha256']:
                # The checksum also catches accidental overwrites of prior SPEs.
                raise RuntimeError('Previously saved SPE changed: ' + frame['spe_path'])
        modes = {}
        for mode in ('fresh', 'reuse'):
            frames = [f for f in report['frames'] if f['mode'] == mode]
            modes[mode] = dict(start_median_s=median([f['bridge_timing_s']['start_experiment'] for f in frames]),
                               capture_median_s=median([f['capture_s'] for f in frames]))
        report['summary'] = modes
        report['start_saved_s'] = modes['fresh']['start_median_s'] - modes['reuse']['start_median_s']
        report['status'] = 'complete'
    except BaseException as exc:
        report['status'] = 'failed'
        report['error'] = '%s: %s' % (type(exc).__name__, exc)
        if not br.TEMPERATURE_MONITOR_UNHEALTHY.is_set():
            try:
                exp.Stop()
            except Exception as stop_error:
                report['stop_error'] = str(stop_error)
                br.TEMPERATURE_MONITOR_UNHEALTHY.set()
        # Failed documents and all files stay available for recovery.
    finally:
        br.start_new_document, br.SPE_PATH = old_start, old_path
        try:
            if br.TEMPERATURE_MONITOR_UNHEALTHY.is_set():
                raise RuntimeError('Stop is stalled; settings restoration skipped until recovery')
            restored = br.apply_settings(exp, restore)
            if any(restored.get(k) != v for k,v in restore.items()):
                raise RuntimeError('Probe settings restoration mismatch')
            br.validate_temperature(restored)
            report['restored_settings'] = restored
            report['restore_ok'] = True
        except BaseException as restore_error:
            report['status'] = 'failed'
            report['restore_error'] = '%s: %s' % (type(restore_error).__name__, restore_error)
        try:
            persist(report)
        except Exception as write_error:
            report['status'] = 'failed'
            report['report_write_error'] = str(write_error)
    return report


def main():
    # Bind before importing COM or writing camera settings. No listener is started.
    lease = reserve_bridge_port()
    try:
        sys.path.insert(0, r'C:\WinSpecRemote')
        import camera_server as br
        if br.ACQUISITION_SETTINGS_VERSION != 2 or 'startup-performance-v11' not in br.SERVER_BUILD:
            raise RuntimeError('Install the verified v11 bridge before this probe')
        stamp = time.strftime('%Y%m%d-%H%M%S') + '-%d' % os.getpid()
        output_dir = os.path.join(r'C:\WinSpecRemote', 'startup-probe-' + stamp)
        os.makedirs(output_dir)
        shared_report = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'result-' + stamp + '.json')
        def persist(report):
            report['server_build'] = br.SERVER_BUILD
            persist_reports(report, output_dir, shared_report)
        br.pythoncom.CoInitialize()
        try:
            report = run_probe(br, output_dir, persist)
        finally:
            # Release any failed document reference in its owning apartment.
            # Do not close the WinSpec window or delete its recovery SPE.
            br.TRANSFER_STATE.pending = None
            br.pythoncom.CoUninitialize()
        print(json.dumps(dict((k,report.get(k)) for k in
                              ('status','summary','start_saved_s','restore_ok','error','restore_error')), indent=2))
        print('Local SPE files and report: ' + output_dir)
        print('Shared report: ' + shared_report)
        return 0 if report['status'] == 'complete' and report['restore_ok'] else 1
    finally:
        lease.close()


if __name__ == '__main__':
    try:
        sys.exit(main())
    except Exception as error:
        print('PROBE ABORTED: ' + str(error))
        sys.exit(1)
