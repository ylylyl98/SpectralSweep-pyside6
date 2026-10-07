from __future__ import print_function

"""One-time XP startup investigation: getters only; no acquisition or settings writes."""
import json
import os
import socket
import sys
import time


# Resolve against the installed type library. Never guess numeric enum values.
PARAMETERS = (
    'EXP_CONTROLLER_NAME', 'EXP_CLASSNAME', 'EXP_CCD_CHIP_NAME', 'EXP_DEVNAME',
    'EXP_DRIVERVERSION', 'EXP_CONTROLLER_VERSION', 'EXP_INTERFACE_CARD',
    'EXP_DMASIZE_VIA_DRIVER', 'EXP_DELAY_TIME', 'EXP_READOUT_TIME', 'EXP_ETACTUAL',
    'EXP_NUMBER_OF_CLEANS', 'EXP_NUM_OF_STRIPS_PER_CLN', 'EXP_CONT_CLNS',
    'EXP_SHUTTER_TYPE', 'EXP_SHUTTER_CONTROL', 'EXP_CUSTOM_SHUTTER',
    'EXP_SHUTTER_COMP_TIME_MS', 'EXP_BSHOWWINDOW', 'EXP_NEWWINDOW',
    'EXP_DATA_COLLECTION_MODE', 'EXP_DATA_COLLECTION_TYPE',
    'EXP_CURRENT_READOUT_MODE', 'EXP_NUMREPEATSEXP',
    'EXP_BBACKSUBTRACT', 'EXP_BDOFLATFIELD', 'EXP_DOCOSMIC',
)
SNAPSHOT_PARAMETERS = (
    ('exposure_ms', 'EXP_EXPOSURE', lambda v: float(v) * 1000.),
    ('accumulations', 'EXP_ACCUMS', int),
    ('sequential_frames', 'EXP_SEQUENTS', int),
    ('timing_mode', 'EXP_TIMING_MODE', int),
    ('temperature_setpoint_c', 'EXP_TEMPERATURE', float),
    ('adc_rate', 'EXP_ADC_RATE', int), ('controller_gain', 'EXP_GAIN', int),
    ('shutter_control', 'EXP_SHUTTER_CONTROL', int),
    ('roi_enabled', 'EXP_USEROI', bool),
    ('detector_width', 'EXP_XDIMDET', int), ('detector_height', 'EXP_YDIMDET', int),
    ('output_width', 'EXP_XDIM', int), ('output_height', 'EXP_YDIM', int),
    ('controller_running', 'EXP_RUNNING', bool),
    ('winspec_reported_running', 'EXP_RUNNING_EXPERIMENT', bool),
    ('actual_temperature_c', 'EXP_ACTUAL_TEMP', float),
    ('temperature_locked', 'EXP_TEMP_STATUS', bool),
)
CONFIGURATION_KEYS = tuple(item[0] for item in SNAPSHOT_PARAMETERS[:13])


def clock():
    return (getattr(time, 'monotonic', None) or time.clock)()


def require_idle(settings):
    if any(settings.get(k) for k in
           ('running', 'winspec_reported_running', 'controller_running')):
        raise RuntimeError('WinSpec is busy; audit aborted without writes')
    if not any(settings.get(k) is False for k in
               ('winspec_reported_running', 'controller_running')):
        raise RuntimeError('Cannot verify WinSpec idle; audit aborted without writes')


def snapshot(br, exp):
    settings, errors = {}, {}
    for key, name, convert in SNAPSHOT_PARAMETERS:
        try:
            enum_value = br.const(name)
            if enum_value is None:
                raise RuntimeError('Enum missing from installed type library')
            value = br.get_param(exp, enum_value)
            json.dumps(value, allow_nan=False)
            settings[key] = convert(value)
        except Exception as error:
            errors[key] = '%s: %s' % (type(error).__name__, error)
    return settings, errors


def run_audit(br):
    started = clock()
    exp = br.create_experiment()
    factory_s = clock() - started
    before, before_errors = snapshot(br, exp)
    require_idle(before)
    report = dict(status='collecting', server_build=br.SERVER_BUILD,
                  scope='GetParam only; no Start, Stop, SetParam, DocFile or spectrometer calls',
                  create_experiment_s=factory_s, settings_before=before,
                  settings_before_errors=before_errors, parameters={})
    for name in PARAMETERS:
        enum_value = br.const(name)
        if enum_value is None:
            report['parameters'][name] = dict(status='unsupported', error='Enum missing from installed type library')
            continue
        tick = clock()
        try:
            value = br.get_param(exp, enum_value)
            # Unsupported COM objects/nonfinite numbers must not masquerade as values.
            json.dumps(value, allow_nan=False)
            entry = dict(status='read', value=value)
        except Exception as error:
            entry = dict(status='unsupported', error='%s: %s' % (type(error).__name__, error))
        entry['get_param_s'] = clock() - tick
        report['parameters'][name] = entry
    after, after_errors = snapshot(br, exp)
    report['settings_after'] = after
    report['settings_after_errors'] = after_errors
    same = all(before.get(k) == after.get(k) for k in CONFIGURATION_KEYS)
    report['settings_unchanged'] = same
    report['settings_comparison_scope'] = [k for k in CONFIGURATION_KEYS if k in before and k in after]
    report['status'] = 'complete' if same else 'failed'
    if not same:
        report['error'] = 'Configuration changed externally during the read-only audit; no restoration attempted'
    try:
        require_idle(after)
    except RuntimeError as error:
        report['status'] = 'failed'
        report['error'] = str(error)
    return report


def reserve_port():
    lease = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        if hasattr(socket, 'SO_EXCLUSIVEADDRUSE'):
            lease.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        lease.bind(('0.0.0.0', 5000))
    except Exception:
        lease.close()
        raise RuntimeError('Close only the camera SERVER window first; port 5000 is occupied')
    return lease


def save_report(report, path):
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, 'w') as handle:
        json.dump(report, handle, indent=2, allow_nan=False)
        handle.flush()
        os.fsync(handle.fileno())


def main():
    lease = reserve_port()  # Exclusivity before importing COM; no server starts.
    try:
        sys.path.insert(0, r'C:\WinSpecRemote')
        import camera_server as br
        stamp = time.strftime('%Y%m%d-%H%M%S') + '-%d' % os.getpid()
        br.pythoncom.CoInitialize()
        try:
            report = run_audit(br)
        finally:
            br.pythoncom.CoUninitialize()
        name = 'startup-audit-' + stamp + '.json'
        local = os.path.join(r'C:\WinSpecRemote', name)
        save_report(report, local)
        shared = os.path.join(os.path.dirname(os.path.abspath(__file__)), name)
        save_report(report, shared)
        print(json.dumps(report, indent=2, allow_nan=False))
        print('Local report: ' + local)
        print('Shared report: ' + shared)
        return 0 if report['status'] == 'complete' else 1
    finally:
        lease.close()


if __name__ == '__main__':
    try:
        sys.exit(main())
    except Exception as error:
        print('AUDIT ABORTED: ' + str(error))
        sys.exit(1)
