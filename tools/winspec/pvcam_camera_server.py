from __future__ import print_function

"""Optional XP/Python 2.7 PVCAM service; no WinSpec COM or optics control.

Uses the native ABI/serialized worker already validated by the startup probe.
Summed float64 transport counts are normalized once by the existing host.
"""
import argparse
import ctypes as ct
import hashlib
import json
import math
import os
import socket
import struct
import subprocess
import threading
import time
import uuid
try:
    import SocketServer as socketserver
except ImportError:
    import socketserver
try:
    from .pvcam_startup_probe import (NativePVCAM, RecoveryRequired, write_raw,
        find_pvcam_dll, WinSpecDriverEnvironment, hold_for_recovery, clock)
    from .temperature_guard import TemperatureGuard
except (ImportError, ValueError):
    from pvcam_startup_probe import (NativePVCAM, RecoveryRequired, write_raw,
        find_pvcam_dll, WinSpecDriverEnvironment, hold_for_recovery, clock)
    from temperature_guard import TemperatureGuard

BUILD = '2026-10-01-pvcam-backend-v2'
FINISH_EACH_BUILD = BUILD + '-finish-each'
ARCHIVE_PLACEBO_BUILD = BUILD + '-archive-placebo'
MINIMUM_DURATION_TOLERANCE_S = .020
REQUEST = struct.Struct('<4sHI')
RESPONSE = struct.Struct('<4sHII')


def timing_snapshot():
    # Three independent API readings for diagnosing XP/VM timing discrepancies.
    # GetTickCount is a wrapping 32-bit millisecond counter on XP.
    return dict(qpc_s=clock(), unix_s=time.time(),
        tick_ms=(int(ct.windll.kernel32.GetTickCount()) & 0xffffffff) if os.name == 'nt' else None)


def integer(value, name, low, high):
    if (isinstance(value, bool) or not isinstance(value, (int, float)) or
            math.isnan(value) or math.isinf(value) or int(value) != value or
            not low <= value <= high):
        raise ValueError('%s must be an integer in %d..%d' % (name, low, high))
    return int(value)


class PVCAMBridge(object):
    def __init__(self, sdk, output_dir, restore_recipe, expected_native,
                 diagnostic_finish_each=False, diagnostic_archive_placebo=False):
        if diagnostic_finish_each and diagnostic_archive_placebo:
            raise ValueError('Diagnostic cleanup modes must be mutually exclusive')
        self.sdk, self.output_dir = sdk, output_dir
        self.restore_recipe = dict(restore_recipe)
        self.expected_native = dict(expected_native)
        self.lock = threading.Lock()
        self.stop = threading.Event()
        self.closing = threading.Event()
        self.active = threading.Event()
        self.pending_transfer = None
        self.unhealthy = False
        self.prepared = False
        self.readout_completed = False
        self.setup_attempted = False
        self.may_run = False
        self.closed = False
        self.settings = {}
        self.timing = timing_snapshot
        self.diagnostic_finish_each = diagnostic_finish_each
        self.diagnostic_archive_placebo = diagnostic_archive_placebo
        self.diagnostic_archive = diagnostic_finish_each or diagnostic_archive_placebo
        self.build = FINISH_EACH_BUILD if diagnostic_finish_each else ARCHIVE_PLACEBO_BUILD if diagnostic_archive_placebo else BUILD

    def initialize(self):
        integer(self.restore_recipe.get('exposure_ms'), 'restoration exposure', 1, 65535)
        if self.restore_recipe.get('accumulations') != 1 or self.restore_recipe.get('sequential_frames') != 1:
            raise RuntimeError('Original WinSpec recipe must contain one exposure/frame for restoration')
        self.sdk.open()
        self.original = self.sdk.snapshot()
        for key, value in dict(ser_size=512, par_size=1, bit_depth=16, exp_res=0, exposure_mode=0).items():
            if self.original.get(key) != value:
                raise RuntimeError('Unsupported native geometry/timing: ' + key)
        for key, value in self.expected_native.items():
            if self.original.get(key) != value:
                raise RuntimeError('Native setting changed from validated preflight: ' + key)
        if self.original['temp_setpoint'] != self.restore_recipe['temperature_setpoint_c'] * 100:
            raise RuntimeError('Cooling setpoint changed on native open')
        # These are cached accepted setup_seq inputs, not an exposure getter.
        self.settings = dict(exposure_ms=int(self.restore_recipe['exposure_ms']),
            accumulations=1, sequential_frames=1, timing_mode=1,
            detector_width=512, detector_height=1, output_width=512, output_height=1,
            roi_enabled=False, controller_gain=self.original.get('gain_index'),
            native_speed_index=self.original.get('spdtab_index'),
            pixel_time_ns=self.original.get('pix_time'),
            adc_offset=self.original.get('adc_offset'),
            native_settings=dict(self.original), running=False,
            winspec_reported_running=False, controller_running=False,
            temperature_setpoint_c=self.original['temp_setpoint']/100.,
            temperature_locked=False)
        self.settings['actual_temperature_c'] = self.sdk.cold()
        self._configure({})

    def _healthy(self):
        if self.sdk.unsafe:
            raise RecoveryRequired('Native state uncertain; retain service resources')
        if self.closed or self.closing.is_set() or self.unhealthy:
            raise RuntimeError('PVCAM service requires recovery/restart')

    def _idle_transfer(self):
        if self.pending_transfer:
            raise RuntimeError('Previous transfer has not been acknowledged')

    def _refresh(self):
        current = self.sdk.snapshot()
        if current != self.original:
            self.unhealthy = True
            raise RuntimeError('Native settings changed; acquisition prohibited')
        self.settings['actual_temperature_c'] = self.sdk.cold()

    def _reply(self, busy=False):
        return dict(ok=True, server='pvcam-camera', server_build=self.build,
            acquisition_backend='pvcam', acquisition_settings_version=2,
            temperature_guard_version=4, camera_busy=busy,
            native_health='recovery_required' if self.sdk.unsafe or self.unhealthy else 'ready',
            settings=dict(self.settings))

    def _finish_prepared(self):
        if self.prepared:
            if self.readout_completed:
                self.sdk.finish()
            self.prepared = False
            self.readout_completed = False
            self.sdk.release_buffer()

    def _configure(self, parameters):
        if set(parameters) - set(('exposure_ms', 'accumulations', 'sequential_frames')):
            raise ValueError('Only exposure, accumulations and one sequential frame may be set')
        exposure = integer(parameters.get('exposure_ms', self.settings['exposure_ms']), 'exposure_ms', 1, 65535)
        frames = integer(parameters.get('accumulations', self.settings['accumulations']), 'accumulations', 1, 64)
        integer(parameters.get('sequential_frames', 1), 'sequential_frames', 1, 1)
        self._refresh()
        if self.prepared and not self.stop.is_set() and (exposure, frames) == (self.settings['exposure_ms'], self.settings['accumulations']):
            return
        self._finish_prepared()
        # Mark before setup so a partially configured sequence is retained on failure.
        self.prepared = True
        self.setup_attempted = True
        try:
            size = self.sdk.setup(frames, exposure)
            if size != 1024 * frames:
                raise RuntimeError('Native buffer size differs from full 512 x 1 uint16 frames')
            self.sdk.pin(size)
            self.settings.update(exposure_ms=exposure, accumulations=frames, sequential_frames=1)
            if not self.closing.is_set(): self.stop.clear()
        except BaseException:
            self.unhealthy = True
            raise

    def execute(self, command, parameters=None):
        parameters = parameters or {}
        if not isinstance(parameters, dict): raise ValueError('Parameters must be an object')
        command = command.upper()
        if command == 'STOP':
            self.stop.set()
            return dict(ok=True, stop_requested=self.active.is_set(), acquisition_backend='pvcam'), b''
        if command not in ('HELLO','GET_SETTINGS','GET_ACQUISITION_SETTINGS','GET_STATUS','SET_SETTINGS','ACQUIRE_GUARDED'):
            raise ValueError('Unsupported PVCAM command: ' + command)
        if not self.lock.acquire(False):
            if command in ('GET_STATUS','GET_SETTINGS','GET_ACQUISITION_SETTINGS','HELLO'):
                return self._reply(True), b''
            raise RuntimeError('PVCAM is busy')
        try:
            self._healthy()
            self._idle_transfer()
            if command in ('GET_SETTINGS','GET_ACQUISITION_SETTINGS','GET_STATUS','HELLO'):
                self._refresh()
                return self._reply(), b''
            if command == 'SET_SETTINGS':
                self._configure(parameters)
                return self._reply(), b''
            return self._acquire(parameters)
        finally:
            self.lock.release()

    def _acquire(self, parameters):
        if self.stop.is_set(): raise RuntimeError('PVCAM stopped; apply settings before restarting')
        if not self.prepared: raise RuntimeError('PVCAM sequence is not prepared')
        expected = parameters.get('expected_settings')
        if parameters.get('settings_mode') != 'managed' or not isinstance(expected, dict) or not expected:
            raise ValueError('PVCAM requires managed expected settings')
        for key, value in expected.items():
            if key not in self.settings or self.settings[key] != value:
                raise RuntimeError('PVCAM expected setting differs: ' + key)
        required = ('exposure_ms','accumulations','sequential_frames','timing_mode',
                    'detector_width','detector_height','output_width','output_height','roi_enabled')
        if any(key not in expected for key in required):
            raise ValueError('Incomplete expected acquisition settings')
        def temperature():
            value = self.sdk.cold()
            self.settings['actual_temperature_c'] = value
            return dict(actual_temperature_c=value, temperature_locked=False)
        # No driver parameter/temperature reads during an exposure sequence.
        guard = TemperatureGuard(temperature, lambda: None, clock=clock, boundary_only=True)
        self.active.set()
        started = clock()
        try:
            guard.check()
            if self.stop.is_set() or self.closing.is_set(): raise RuntimeError('PVCAM stopped before start')
            self.may_run = True
            self.readout_completed = False
            start = clock()
            sequence_start = self.timing()
            trace = []
            trace_truncated = False
            self.sdk.start()
            start_duration = clock()-start
            deadline = started + max(30., self.settings['exposure_ms']/1000.*self.settings['accumulations']*1.5+30.)
            size = self.settings['accumulations'] * 1024
            while True:
                if self.stop.is_set(): raise RuntimeError('PVCAM acquisition stopped')
                status, arrived = self.sdk.status()
                if not trace or (status, arrived) != (trace[-1]['status'], trace[-1]['bytes_arrived']):
                    observed = self.timing()
                    entry = dict(status=status, bytes_arrived=arrived, observed=observed)
                    if len(trace) < 64: trace.append(entry)
                    else:
                        trace[-1] = entry
                        trace_truncated = True
                if self.stop.is_set(): raise RuntimeError('PVCAM acquisition stopped')
                if arrived > size or status == 4:
                    raise RuntimeError('PVCAM failed readout or extra bytes')
                if status == 3:
                    if arrived != size: raise RuntimeError('PVCAM incomplete byte count')
                    self.may_run = False
                    self.readout_completed = True
                    sequence_complete = observed
                    break
                if status not in (1,2,5): raise RuntimeError('PVCAM unexpected status')
                if clock() > deadline: raise RuntimeError('PVCAM acquisition timed out')
                time.sleep(.002)
            guard.check()
            capture_duration = clock()-started
            elapsed_qpc = sequence_complete['qpc_s']-sequence_start['qpc_s']
            tick_start, tick_end = sequence_start['tick_ms'], sequence_complete['tick_ms']
            elapsed_tick = ((tick_end-tick_start) & 0xffffffff)/1000. if tick_start is not None and tick_end is not None else None
            nominal = self.settings['exposure_ms']/1000.*self.settings['accumulations']
            duration_passed = (not math.isnan(elapsed_qpc) and not math.isinf(elapsed_qpc)
                and elapsed_qpc + MINIMUM_DURATION_TOLERANCE_S >= nominal)
            sequence_timing = dict(start=sequence_start, complete=sequence_complete,
                elapsed_qpc_s=elapsed_qpc, elapsed_tick_s=elapsed_tick,
                elapsed_unix_s=sequence_complete['unix_s']-sequence_start['unix_s'],
                status_trace=trace, status_trace_truncated=trace_truncated)
            if not duration_passed:
                # Full-byte COMPLETE is known: preserve evidence and stop reuse,
                # but do not abort finished readout or mark driver ownership unsafe.
                self.unhealthy = True
            raw = self.sdk.raw()
            if len(raw) != size: raise RuntimeError('PVCAM raw buffer length mismatch')
            values = struct.unpack('<%dH' % (size//2), raw)
            sums = [sum(values[pixel::512]) for pixel in range(512)]
            payload = struct.pack('<512d', *sums)
            path = os.path.join(self.output_dir, 'capture-'+uuid.uuid4().hex+'.raw')
            write_raw(path, raw)
            with open(path, 'rb') as handle:
                if handle.read() != raw: raise RuntimeError('Raw archive verification failed')
            metadata = self._reply()
            metadata.update(width=512, height=1, frame_count=1, winspec_datatype=5,
                settings_scope='native_setup_plus_raw', receipt_required=True,
                raw_archive=path, raw_sha256=hashlib.sha256(raw).hexdigest(),
                payload_sha256=hashlib.sha256(payload).hexdigest(),
                archive_retention='permanent', raw_frame_count=self.settings['accumulations'],
                accumulation_method='software_sum_of_raw_frames',
                native_settings=dict(self.original), temperature_guard=guard.report(),
                sequence_timing=sequence_timing,
                minimum_duration_guard=dict(version=1, passed=duration_passed,
                    nominal_exposure_total_s=nominal, tolerance_s=MINIMUM_DURATION_TOLERANCE_S,
                    scope='Software duration sanity check; not independent physical exposure proof'),
                exposure_verification='setup_seq accepted; no independent timed exposure getter',
                bridge_timing_s=dict(start_experiment=start_duration,
                    start_native=getattr(self.sdk,'last_start_native_s',start_duration),
                    capture=capture_duration, total_before_transfer=clock()-started))
            if not duration_passed:
                metadata.update(ok=False, native_health='validation_failed',
                    error='PVCAM sequence duration shorter than nominal exposure total; raw evidence retained; restart required')
            elif self.diagnostic_archive:
                # Diagnostic only: preserve pixels before asking the SDK to clean
                # up completed readout. Keep setup and the pinned buffer for Start.
                metadata.update(ok=False, native_health='cleanup_pending')
                metadata['diagnostic_sequence_cleanup'] = dict(
                    policy='archive-matched completed-readout control; reuse setup and buffer',
                    native_finish_called=False, completed=False)
            evidence_path = path[:-4]+('.cleanup-pending.json' if self.diagnostic_archive and duration_passed else '.json')
            write_raw(evidence_path, json.dumps(metadata,allow_nan=False,indent=2).encode('utf-8'))
            if not duration_passed: raise RuntimeError(metadata['error']+'; archive '+path)
            if self.diagnostic_archive:
                finish_started = clock()
                if self.diagnostic_finish_each:
                    metadata['diagnostic_sequence_cleanup']['native_finish_called'] = True
                    try:
                        self.sdk.finish()
                    except BaseException as error:
                        self.unhealthy = True
                        metadata.update(ok=False, native_health='recovery_required',
                            error='Completed-readout diagnostic finish failed: '+str(error))
                        write_raw(path[:-4]+'.json', json.dumps(metadata,allow_nan=False,indent=2).encode('utf-8'))
                        raise
                    self.readout_completed = False  # Do not finish it twice on close/reconfigure.
                # Placebo preserves readout_completed so ordinary close/configure
                # still finishes the last completed readout exactly once.
                metadata.update(ok=True, native_health='ready')
                metadata['diagnostic_sequence_cleanup'].update(completed=True,
                    native_finish_called=bool(self.diagnostic_finish_each),
                    finish_s=clock()-finish_started)
                metadata['bridge_timing_s']['total_before_transfer'] = clock()-started
                write_raw(path[:-4]+'.json', json.dumps(metadata,allow_nan=False,indent=2).encode('utf-8'))
            self.pending_transfer = path
            return metadata, payload
        except BaseException:
            self.stop.set()
            if self.may_run and not self.sdk.unsafe:
                self.sdk.abort()
                self.may_run = False
                self.readout_completed = False
            raise
        finally:
            self.active.clear()

    def complete_transfer(self, acknowledged):
        with self.lock:
            if not acknowledged:
                self.unhealthy = True
                return False
            self.pending_transfer = None
            return True

    def close(self):
        self.closing.set()
        self.stop.set()  # Signal the owning capture before waiting for its lock.
        with self.lock:
            if self.sdk.unsafe: raise RecoveryRequired('Retain all native resources for recovery')
            if self.closed: return
            if self.may_run:
                self.sdk.abort()
                self.may_run = False
                self.readout_completed = False
            self._finish_prepared()
            if self.setup_attempted:
                size = self.sdk.setup(1, int(self.restore_recipe['exposure_ms']))
                if size != 1024 or self.sdk.snapshot() != self.original:
                    self.sdk.unsafe = True
                    raise RecoveryRequired('Native restoration mismatch; retain process')
                self.sdk.cold()
            self.sdk.close()
            self.closed = True


def recv_exact(sock, size):
    result = bytearray()
    while len(result) < size:
        chunk = sock.recv(size-len(result))
        if not chunk: raise RuntimeError('Incomplete request/receipt')
        result.extend(chunk)
    return bytes(result)


class Handler(socketserver.BaseRequestHandler):
    def handle(self):
        pending = False
        try:
            self.request.settimeout(5.)
            magic, version, size = REQUEST.unpack(recv_exact(self.request, REQUEST.size))
            if magic != b'WXRQ' or version != 1 or not 2 <= size <= 1024*1024:
                raise ValueError('Invalid request header')
            request = json.loads(recv_exact(self.request,size).decode('utf-8'))
            if not isinstance(request,dict) or not isinstance(request.get('command'),type(u'')):
                raise ValueError('Invalid request object')
            metadata, payload = self.server.bridge.execute(request['command'],request.get('parameters',{}))
            pending = metadata.get('receipt_required') is True
        except Exception as error:
            metadata, payload = dict(ok=False,error='%s: %s' % (type(error).__name__,error)), b''
        try:
            body = json.dumps(metadata,allow_nan=False,separators=(',',':')).encode('utf-8')
            self.request.sendall(RESPONSE.pack(b'WXRS',1,len(body),len(payload))+body+payload)
            if pending:
                acknowledged = recv_exact(self.request,4) == b'WXAK'
                cleaned = self.server.bridge.complete_transfer(acknowledged)
                pending = False
                self.request.sendall(b'WXCL' if cleaned else b'WXER')
        finally:
            if pending: self.server.bridge.complete_transfer(False)


class Server(socketserver.ThreadingTCPServer):
    daemon_threads = True
    allow_reuse_address = False
    def server_bind(self):
        if hasattr(socket,'SO_EXCLUSIVEADDRUSE'):
            self.socket.setsockopt(socket.SOL_SOCKET,socket.SO_EXCLUSIVEADDRUSE,1)
        socketserver.ThreadingTCPServer.server_bind(self)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--preflight',required=True)
    cleanup_modes = parser.add_mutually_exclusive_group()
    cleanup_modes.add_argument('--diagnostic-finish-each', action='store_true',
        help='Diagnostic only: finish each completed archived capture, preserving setup/buffer')
    cleanup_modes.add_argument('--diagnostic-archive-placebo', action='store_true',
        help='Diagnostic only: match extra archive writes but omit per-capture native finish')
    args = parser.parse_args()
    if os.name != 'nt' or ct.sizeof(ct.c_void_p) != 4:
        raise RuntimeError('Run with XP C:\\Python27\\python.exe, 32 bit')
    info = subprocess.STARTUPINFO(); info.dwFlags |= subprocess.STARTF_USESHOWWINDOW
    processes = subprocess.check_output(['tasklist','/FO','CSV','/NH'],startupinfo=info)
    if b'"winspec.exe"' in processes.lower():
        raise RuntimeError('Close XP WinSpec and its camera server first; keep controller powered')
    with open(args.preflight,'r') as handle: preflight = json.load(handle)
    age = time.time()-preflight['saved_unix']
    if not 0 <= age <= 3600: raise RuntimeError('Fresh idle restoration preflight required (one hour)')
    service = Server(('0.0.0.0',5000),Handler,bind_and_activate=False)
    sdk = environment = bridge = None
    try:
        service.server_bind()  # Reserve production port before any driver initialization.
        folder = os.path.dirname(os.path.abspath(__file__))
        dll_path, source = find_pvcam_dll(folder)
        environment = WinSpecDriverEnvironment(dll_path); environment.activate()
        sdk = NativePVCAM(dll_path)
        output = os.path.join(r'C:\WinSpecRemote','pvcam-service-'+time.strftime('%Y%m%d-%H%M%S')+'-'+uuid.uuid4().hex[:8])
        os.makedirs(output)
        bridge = PVCAMBridge(sdk,output,preflight['settings'],preflight['expected_native'],
            diagnostic_finish_each=args.diagnostic_finish_each,
            diagnostic_archive_placebo=args.diagnostic_archive_placebo)
        service.bridge = bridge
        bridge.initialize()
        service.server_activate()
        print('PVCAM '+bridge.build+' listening on port 5000; archive '+output)
        service.serve_forever(poll_interval=.2)
    finally:
        if bridge is not None and not sdk.unsafe:
            try: bridge.close()
            except BaseException:
                sdk.unsafe = True
                raise
            finally:
                if sdk.unsafe: hold_for_recovery(sdk,service)
        elif sdk is not None and sdk.unsafe:
            hold_for_recovery(sdk,service)
        elif sdk is not None:
            try: sdk.close()
            except BaseException:
                sdk.unsafe=True
                hold_for_recovery(sdk,service)
        if environment is not None: environment.restore()
        service.server_close()


if __name__ == '__main__':
    try: main()
    except KeyboardInterrupt: print('PVCAM service stopped; original native recipe restored. Recheck WinSpec settings before use.')
